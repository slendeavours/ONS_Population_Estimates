"""Gates for the S6 (Home Office asylum support by local authority, Asy_D11
and Reg_02) editions tables.

Mirrors scripts/s23_rsh_stock_editions_verify.py and
s22_ctb_editions_verify.py (several specs applied together).
Prints `GATE n name: PASS|FAIL`; exits 1 on any FAIL. It writes nothing, not
even a run-log row. The real-table gates read la_asylum_support,
la_asylum_support_unallocated, asylum_support_non_england and
la_immigration_groups, their four *_editions tables and four
*_editions_file_checks ledgers. Until the editions tables exist (ddl and
migrate-legacy not yet run) they print `FAIL (pending migration)` with the
words "not yet migrated", so the script exits non-zero until then, like the
other verifiers; nothing is created to make them pass.

The seeded gates never need the real editions tables: they run on the
throwaway tables zz_s6_support, zz_s6_unallocated, zz_s6_non_england,
zz_s6_groups (copies of the live tables) with their zz_s6_*_editions tables
and ledgers, created inside the transaction, and call the loader's own
writers and commands (s6_asylum_editions.main with the four specs pointed at
the copies, the content API and the downloads stubbed; apply_d11_period,
apply_reg02_period; migrate_legacy; restore_edition) rather than a
re-implementation. The real tables are only ever read.

Gates:
  1  four editions tables, four ledgers, append-only triggers (UPDATE,
     DELETE, TRUNCATE refused), live shape               (throwaway + real)
  2  every live period of each table has edition 1; the latest edition
     equals live cell for cell (load_checks.check_latest_equals_live and an
     exact two-way comparison)                                       (real)
  3  live source_edition uniform per period and equal to the tip's
     release_label                                                   (real)
  4  codes: every lad24cd in la_boundaries, no E08000038/39 in lad24cd,
     non-England lad_code prefixes S12/W06/N09 only, geography through
     geography.resolve, no private recode dict and no private lookup query
     beyond the declared new_unitary read (AST)
  5  rule 1: no NULL or 0 people in the three Asy_D11 tables; in groups
     NULL people iff suppressed iff source_marker '*', no suppressed 0; a
     blank, text or zero cell in a file halts; no source value coerced to 0
  6  reconciliations re-run read-only from the files on disk (pivot cache,
     Asy_D09, per-period UK total, Reg_02 internal and against Asy_D11)
  7  identity from the file (seeded cover, title, Contents and header
     mismatches halt; held files' covers equal the surveyed ranks)
  8  older-file guard on the page, --release and --file paths (Asy_D11 and
     Reg_02), the rank the file's own never its name; equal rank with other
     content stops without --accept-reissue
  9  stop conditions: each seeded REJECTED, stores nothing for the period,
     no ledger row, partial run-log row
 10  a new period: editions, live rows and ledger rows of the three Asy_D11
     tables in one transaction
 11  ledger skip needs the (URL, sha256) pair for every period in every table
 12  preview and simulate write nothing
 13  a byte-identical re-read is unchanged for every period on every path,
     against edition 1 "as loaded" (Reg_02 NULLs and zeros included)
 14  a revision, then refresh-latest: only that period changes, source_edition
     on every row, loaded_at copied; key changes only with
     --accept-key-changes
 15  edition 1 equals the before-state hash lines in
     docs/decisions/2026-10-10-s6-editions-first-load.md             (real)
 16  the migration proof re-run read-only from the files on disk     (real)
 17  rerun idempotent
 18  stranded period repair
 19  restore-edition round trip
 20  asylum_series_breaks equals SERIES_BREAKS (ignoring break_id) and
     vw_la_asylum_support_totals columns unchanged; the loader writes only
     its own tables
 21  no network, no secret in source or output, nothing left committed, a
     download never overwrites a same-named file with different content
 22  the merged-authority name never depends on row order (shuffled and
     reversed rows give the same records and no revision)

Usage:
    python scripts/s6_asylum_editions_verify.py

Everything runs in one transaction that ends in a rollback; each seeded step
also runs in a savepoint that is rolled back (in a finally). The cursor handed
to the gates refuses commit and rollback on its connection, and the commands
get a stand-in connection whose commit only acts on a savepoint. No network,
no real-table writes, no backend is ever terminated. After the rollback a
second, read-only connection checks that no zz% table was left behind.
"""
import ast
import contextlib
import io
import os
import random
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
import load_checks  # noqa: E402
import period_editions as pe  # noqa: E402
import s6_asylum_editions as m  # noqa: E402
import test_s6_asylum_loader as tl  # noqa: E402
from test_s6_asylum_pure import (Q3, Q4, _Session, d11_rows,  # noqa: E402
                                 dtext, write_d09, write_d11, write_reg02)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# the real specs and constants, taken before anything is patched; the
# real-table gates use only these (never m.SPEC_*, which the seeded gates
# point at the copies)
TAGS = m.TAGS
D11 = m.D11_TAGS
REAL = {t: getattr(m, m.SPEC_NAMES[t]) for t in TAGS}
REAL_LAS = m.REG02_LAS
ZZ = tl.ZZ
ED = tl.ED
LEDGER = tl.LEDGER
LOADER = HERE / "s6_asylum_editions.py"
NOTE = (HERE.parent / "docs" / "decisions"
        / "2026-10-10-s6-editions-first-load.md")
NOT_YET = "the S6 editions tables do not exist yet (not yet migrated)"
RESULTS = []
OUTPUT = []  # every line the gates print, for the secret-leak gate (21)
NEVER_CODES = ("E08000038", "E08000039")
NON_ENGLAND_PREFIXES = ("S12", "W06", "N09")
JUNE, MARCH = tl.JUNE, tl.MARCH
P_FIRST, P_REV, P_MAR, P_NEW = ("2025-09-30", "2025-12-31", "2026-03-31",
                                "2026-06-30")
VIEW = "vw_la_asylum_support_totals"
VIEW_COLUMNS = (
    ("period_ending", "date"), ("lad24cd", "text"),
    ("la_name", "character varying"), ("total_supported", "bigint"),
    ("dispersal", "bigint"), ("initial_accommodation", "bigint"),
    ("contingency_hotel", "bigint"), ("contingency_other", "bigint"),
    ("contingency_all", "bigint"), ("other_accommodation", "bigint"),
    ("subsistence_only", "bigint"), ("accommodation_not_stated", "bigint"),
    ("section_4", "bigint"), ("section_95", "bigint"),
    ("section_98", "bigint"), ("source_edition", "text"))


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


def exists(cur, specs=REAL):
    return all(pe.table_exists(cur, t) for s in specs.values()
               for t in (s.editions_table, ledger_name(s)))


def _need(cur, spec):
    miss = [t for t in (spec.editions_table, ledger_name(spec))
            if not pe.table_exists(cur, t)]
    if miss:
        raise ValueError(f"{', '.join(miss)} does not exist")


@contextmanager
def las(n=REAL_LAS):
    with mock.patch.object(m, "REG02_LAS", n):
        yield


# ------------------------------------------------------------ throwaway data

def setup_throwaway(cur):
    cur.execute("SELECT " + ", ".join(f"to_regclass('public.{n}')"
                                      for n in tl.ALL))
    if any(cur.fetchone()):
        sys.exit("HARD STOP: a zz_s6 table already exists as a real table; "
                 "refusing to run")
    for t in TAGS:
        cur.execute(f"CREATE TABLE public.zz_s6_{t} (LIKE "
                    f"public.{tl.LIVE[t]} INCLUDING ALL)")
    with tl.specs():
        m.create_all(cur)


def state(cur):
    """Everything a refused or previewed run must leave alone: the four live
    tables, the four editions tables and the four ledgers."""
    out = []
    for t in tl.ALL:
        cur.execute(f"SELECT md5(string_agg(t::text, ',' ORDER BY t::text)) "
                    f"FROM public.{t} t")
        out.append(cur.fetchone()[0])
    return tuple(out)


def tables_state(cur):
    """state() without the ledgers (a re-read adds 'unchanged' rows)."""
    return state(cur)[:8]


def period_state(cur, period):
    """Row counts of a period in every live table, editions table and ledger."""
    out = []
    for t in tl.ALL:
        cur.execute(f"SELECT COUNT(*) FROM public.{t} WHERE period_ending = "
                    "%s", (period,))
        out.append(cur.fetchone()[0])
    return tuple(out)


# ------------------------------------------------------------ files and Env

class Env(tl.Fixture):
    """The loader's main(argv) on the throwaway tables, the content API and
    the downloads stubbed; every workbook is written here (the writers of
    test_s6_asylum_pure)."""

    URL = tl.Migrate.URL
    seed_legacy = tl.Migrate.seed_legacy
    migrate = tl.Migrate.migrate

    def __init__(self, cur, root):
        super().__init__()
        self.cur = cur
        self.root = Path(root)
        self.tables = tl.tables_page(d11=None, d09=None)
        self.regional = tl.regional_page([])
        self.api = {m.TABLES_PATH: self.tables, m.REGIONAL_PATH: self.regional}
        self.urls, self.final, self.fetched = {}, {}, []
        self.n = 0

    # -- files
    def d11_file(self, rows, quarters, *, tag=None, register=True, url=None,
                 published=None):
        """An Asy_D11 and its Asy_D09 from `rows`, listed on the page."""
        self.n += 1
        d = date.fromisoformat(quarters[-1])
        tag = tag or f"{d:%b-%Y}".lower()
        p11 = write_d11(self.root / f"d{self.n}" /
                        f"support-local-authority-datasets-{tag}.xlsx",
                        quarters, rows=rows, published=published)
        p09 = write_d09(self.root / f"d{self.n}" /
                        f"asylum-seekers-receipt-support-datasets-{tag}.xlsx",
                        quarters, rows=rows)
        if register:
            u11 = url or f"https://assets.example/media/m{self.n}/{p11.name}"
            u09 = f"https://assets.example/media/n{self.n}/{p09.name}"
            self.urls[u11], self.urls[u09] = p11, p09
            ye = f"{d:%B %Y}"
            self._set_att(self.tables, "Asylum seekers in receipt of Home "
                          "Office support by local authority",
                          "Asylum seekers in receipt of Home Office support "
                          "by local authority detailed datasets, year ending "
                          f"{ye}", u11)
            self._set_att(self.tables, "Asylum seekers in receipt of Home "
                          "Office support detailed",
                          "Asylum seekers in receipt of Home Office support "
                          f"detailed datasets, year ending {ye}", u09)
            self.u11 = u11
        return p11, p09

    def d11(self, quarters=Q4, *, register=True, url=None, published=None,
            tag=None, **kw):
        return self.d11_file(d11_rows(quarters, **kw), quarters, tag=tag,
                             register=register, url=url, published=published)

    def reg(self, period="2026-06-30", *, register=True, url=None,
            published=None, tag=None, **kw):
        self.n += 1
        d = date.fromisoformat(period)
        name = (f"regional-and-local-authority-dataset-"
                f"{tag or f'{d:%b-%Y}'.lower()}.ods")
        p = write_reg02(self.root / f"r{self.n}" / name, period,
                        published=published, **kw)
        if register:
            u = url or f"https://assets.example/media/r{self.n}/{name}"
            self.urls[u] = p
            ye = f"{d:%B %Y}"
            atts = self.regional["details"]["attachments"]
            atts[:] = [a for a in atts if not a["title"].endswith(ye)]
            atts.append({"title": "Regional and local authority data on "
                         f"immigration groups, year ending {ye}", "url": u})
            self.ureg = u
        return p

    # -- commands
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

    def seed_all(self):
        """Three Asy_D11 quarters (to 2026-03-31, one with an unallocated
        row) and the Reg_02 of 2026-03-31, loaded."""
        self.d11(Q3)
        self.reg("2026-03-31")
        self.ok(self.cur, ["load", "--commit"])
        self.fetched.clear()


@contextmanager
def env(cur):
    with tempfile.TemporaryDirectory() as tmp:
        yield Env(cur, tmp)


def scenario(cur, fn, *, seed=True):
    """Run fn(env) on the throwaway tables in a savepoint that is rolled
    back; seed=True loads the base state first."""
    def body(c):
        with env(c) as e, tl.specs():
            if seed:
                e.seed_all()
            return fn(e)
    return _in_savepoint(cur, body)


def cases(e, items):
    """items: [(label, fn(e) -> problem text or None)], each in its own
    savepoint on top of the current state; the problems."""
    out = []
    for label, fn in items:
        def one(c, label=label, fn=fn):
            bad = fn(e)
            if bad:
                out.append(f"{label}: {bad}")
        _in_savepoint(e.cur, one)
    return out


def legacy_world(cur, fn, **kw):
    """A seeded legacy state (as the old build left it) and the fixture
    files; fn(env, paths, legacy, files)."""
    def body(c):
        with env(c) as e, tl.specs():
            paths, legacy, files = e.seed_legacy(c, **kw)
            return fn(e, paths, legacy, files)
    return _in_savepoint(cur, body)


def mig(e, paths, legacy, files, argv_extra=("--commit",)):
    """migrate-legacy through the command on the fixture state."""
    p11, p09, rm, rj = paths
    argv = ["migrate-legacy", "--d11", p11, "--reg02", rm, "--reg02", rj,
            "--d09", p09, *argv_extra]
    with mock.patch.object(m, "LEGACY_LIVE", legacy), \
            mock.patch.object(m, "LEGACY_FILES", files):
        return e.cmd(argv)


# ---------------------------------------------------------------- gate 1

_DTYPE = {"integer": "integer", "varchar": "character varying",
          "text": "text", "date": "date", "boolean": "boolean",
          "numeric": "numeric"}


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
    bad += _triggers(cur, spec.editions_table, spec.trigger,
                     spec.truncate_trigger)
    for c in tuple(spec.key_cols) + (spec.period_col, "source_edition",
                                     "loaded_at"):
        if c not in lcols:
            bad.append(f"{spec.live_table}: {c} missing")
    # the live table keeps the held nullability (rule 1: people NOT NULL in
    # the three Asy_D11 tables, nullable only in groups, with suppressed)
    for c, ty in spec.value_cols + spec.extra_cols:
        want = "NO" if "NOT NULL" in ty.upper() else "YES"
        if c in lcols and lcols[c][1] != want:
            bad.append(f"{spec.live_table}: {c} nullable is {lcols[c][1]}, "
                       f"expected {want}")
    for c in ("source_edition", "loaded_at"):
        if c in lcols and lcols[c][1] != "NO":
            bad.append(f"{spec.live_table}: {c} must be NOT NULL")
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


def immutability(cur, stmts):
    """{UPDATE, DELETE, TRUNCATE: error text or None}; all in savepoints."""
    out = {}
    for k, stmt in stmts.items():
        cur.execute("SAVEPOINT stmt")
        try:
            cur.execute(stmt)
            out[k] = None
        except psycopg2.Error as e:
            out[k] = str(e).splitlines()[0]
        finally:
            cur.execute("ROLLBACK TO SAVEPOINT stmt")
            cur.execute("RELEASE SAVEPOINT stmt")
    return out


def gate_1_table_shape_and_immutability(cur):
    name = ("four editions tables, four ledgers, live shape (UPDATE, DELETE "
            "and TRUNCATE are refused on every editions table and ledger)")
    bad = []
    for t in TAGS:
        bad += [f"{t}: {p}" for p in shape_problems(cur, ZZ[t])]

    def body(e):
        for t in TAGS:
            for what, tab in (("editions", ED[t]), ("ledger", LEDGER[t])):
                col = "edition" if what == "editions" else "outcome"
                val = "edition" if what == "editions" else "'new'"
                stmts = {"UPDATE": f"UPDATE public.{tab} SET {col} = {val}",
                         "DELETE": f"DELETE FROM public.{tab}",
                         "TRUNCATE": f"TRUNCATE public.{tab}"}
                for k, msg in immutability(e.cur, stmts).items():
                    if not (msg and "append-only" in msg):
                        bad.append(f"{what} {tab} {k} not refused ({msg})")
            e.cur.execute(f"SELECT COUNT(*) FROM public.{ED[t]}")
            if not e.cur.fetchone()[0]:
                bad.append(f"{ED[t]}: the seeded state holds no rows, so the "
                           "refusal was not tested on data")
    scenario(cur, body)
    scope = "throwaway copies"
    if exists(cur):
        scope += " and real tables"
        for t in TAGS:
            bad += [f"real {t}: {p}" for p in shape_problems(cur, REAL[t])]
    report(1, name, not bad, "; ".join(bad[:4]) if bad else
           f"checked on the {scope}; UPDATE/DELETE/TRUNCATE raise "
           "append-only on the four editions tables and the four ledgers")


# ------------------------------------------------------- real-table gates

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


def real_edition1_and_latest(cur, specs=REAL):
    bad, parts = [], []
    for tag, spec in specs.items():
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
            bad.append(f"{spec.live_table}: no edition 1 (with a source_file) "
                       f"for {missing[:5]} ({len(missing)} of "
                       f"{len(periods)})")
            continue
        cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                    f"public.{spec.editions_table}")
        stray = sorted(str(r[0]) for r in cur.fetchall()
                       if str(r[0]) not in set(periods))
        if stray:
            bad.append(f"{spec.editions_table}: periods with no live rows "
                       f"{stray[:5]}")
        cols = ", ".join(spec.data_cols)
        for p in periods:
            ed = _tip(cur, spec, p)
            a, b = _both_ways(
                cur, f"SELECT {cols} FROM public.{spec.editions_table} WHERE "
                f"{spec.period_col} = %s AND edition = %s", (p, ed),
                f"SELECT {cols} FROM public.{spec.live_table} WHERE "
                f"{spec.period_col} = %s", (p,))
            if a or b:
                bad.append(f"{spec.live_table} {p} ed{ed}: {a} edition-only, "
                           f"{b} live-only rows")
        bad += [f"{spec.live_table}: {x}" for x in
                load_checks.check_latest_equals_live(cur, spec)]
        parts.append(f"{spec.live_table} {len(periods)}")
    return not bad, "; ".join(bad[:3]) if bad else (
        "every live period has edition 1 and the latest edition equals live "
        "cell for cell (" + ", ".join(parts) + " periods)")


def real_provenance(cur, specs=REAL):
    """Live source_edition uniform per period and equal to the tip's
    release_label; every tip's source_file names the file's year-ending and
    published date (its rank)."""
    bad, parts = [], []
    for tag, spec in specs.items():
        _need(cur, spec)
        periods = _live_periods(cur, spec)
        if not periods:
            return False, f"{spec.live_table}: {EMPTY}"
        for p in periods:
            tip = _tip(cur, spec, p)
            cur.execute(f"SELECT DISTINCT source_edition FROM public."
                        f"{spec.live_table} WHERE {spec.period_col} = %s",
                        (p,))
            got = [r[0] for r in cur.fetchall()]
            cur.execute(f"SELECT DISTINCT release_label, source_file FROM "
                        f"public.{spec.editions_table} WHERE "
                        f"{spec.period_col} = %s AND edition = %s", (p, tip))
            want = cur.fetchall()
            if len(got) != 1:
                bad.append(f"{spec.live_table} {p}: live source_edition is "
                           f"not uniform ({len(got)} distinct values)")
            elif len(want) != 1:
                bad.append(f"{spec.editions_table} {p} ed{tip}: {len(want)} "
                           "distinct release labels or source files")
            elif got[0] != want[0][0]:
                bad.append(f"{spec.live_table} {p}: live source_edition "
                           f"{got[0]!r} differs from the tip's "
                           f"{want[0][0]!r}")
            elif m.release_rank(want[0][1]) is None:
                bad.append(f"{spec.editions_table} {p} ed{tip}: source_file "
                           "names no year-ending and published date")
        parts.append(f"{spec.live_table} {len(periods)}")
    return not bad, "; ".join(bad[:3]) if bad else (
        "live source_edition is uniform in every period of the four tables "
        "and the tip's release_label; every tip names its file's rank ("
        + ", ".join(parts) + " periods)")


def _source_problems(path):
    """Problems in a module's source (an AST check): a string constant,
    other than a docstring, holding a Barnsley/Sheffield code (a private
    recode dict); a string constant that queries la_code_lookup other than
    the declared new_unitary read; no call of geography.resolve; a source
    value coerced to 0 (`x or 0`, COALESCE(x, 0), `else 0`) on a line
    without `# not a source value`."""
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
    query = re.compile(r"(?is)\bselect\b.*\bfrom\b|\bupdate\b.*\bset\b|"
                       r"\binsert\s+into\b|\bdelete\s+from\b")
    bad, resolves = [], 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func,
                                                     ast.Attribute) \
                and node.func.attr == "resolve" \
                and isinstance(node.func.value, ast.Name) \
                and node.func.value.id == "geography":
            resolves += 1
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in doc:
            if pat.search(node.value):
                bad.append(f"line {node.lineno}: private code "
                           f"{node.value[:40]!r}")
            if "la_code_lookup" in node.value and query.search(node.value) \
                    and "change_type = 'new_unitary'" not in node.value:
                bad.append(f"line {node.lineno}: la_code_lookup query "
                           "beyond the declared new_unitary read")
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
    if not resolves and "geography" in text:
        bad.append("no call of geography.resolve")
    return bad


def real_codes(cur, specs=REAL, loader=LOADER, form="mixed"):
    for spec in specs.values():
        _need(cur, spec)
    cur.execute("SELECT lad24cd FROM public.la_boundaries")
    valid = {r[0] for r in cur.fetchall()}
    bad, seen = [], 0
    for tag in ("support", "groups"):
        spec = specs[tag]
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
                bad.append(f"{table}: lad24cd holds {new} (Barnsley/"
                           "Sheffield new codes resolve to E08000016/19)")
        if not _live_periods(cur, spec):
            bad.append(f"{spec.live_table}: {EMPTY}")
        resolved = 0
        for p in _live_periods(cur, spec):
            cur.execute(f"SELECT DISTINCT lad24cd FROM public."
                        f"{spec.live_table} WHERE {spec.period_col} = %s",
                        (p,))
            codes = {r[0] for r in cur.fetchall()}
            rmap, problems = m.resolve_codes(cur, codes, {p: codes})
            bad += [f"{spec.live_table} {p}: {x}" for x in problems[:2]]
            off = [c for c in codes if rmap.get(c) != c]
            if off:
                bad.append(f"{spec.live_table} {p}: stored lad24cd that "
                           f"geography.resolve would change {off[:3]}")
            resolved += len(codes)
        seen += resolved
    spec = specs["non_england"]
    for table in (spec.live_table, spec.editions_table):
        cur.execute(f"SELECT DISTINCT lad_code, country FROM public.{table}")
        rows = cur.fetchall()
        off = sorted(c for c, _ in rows
                     if c[:3] not in NON_ENGLAND_PREFIXES)
        if off:
            bad.append(f"{table}: lad_code(s) {off[:4]} outside S12/W06/N09")
        wrong = [(c, k) for c, k in rows
                 if m.COUNTRY_BY_PREFIX.get(c[:3]) != k]
        if wrong:
            bad.append(f"{table}: country does not follow the prefix "
                       f"{wrong[:3]}")
        seen += len(rows)
    src = _source_problems(loader)
    if src:
        bad.append(f"the loader: {src[:2]}")
    decl = geography.DATASET_FORM.get(m.RUN_SOURCE, ("?",))[0]
    if decl != form:
        bad.append(f"source {m.RUN_SOURCE} is declared {decl!r}, expected "
                   f"{form!r}")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{seen} distinct code(s) over live and editions: every lad24cd in "
        "la_boundaries and stable under geography.resolve, no E08000038/39 "
        "in lad24cd, non-England lad_code S12/W06/N09 with the matching "
        f"country; source {m.RUN_SOURCE} declared {form!r}; no private "
        "dict, no la_code_lookup query beyond new_unitary, geography.resolve "
        "called, no value coerced to 0 in the loader")


def gate_4_codes(cur):
    name = ("codes: lad24cd in la_boundaries, no E08000038/39 in lad24cd, "
            "non-England prefixes S12/W06/N09, geography through "
            "geography.resolve, no private recode dict or lookup query")
    out = {}

    def body(e):
        out["base"] = real_codes(e.cur, ZZ)
        e.cur.execute(f"SELECT DISTINCT lad24cd FROM public.zz_s6_support "
                      "WHERE period_ending = %s", (P_MAR,))
        out["barnsley"] = "E08000016" in {r[0] for r in e.cur.fetchall()}
        miss = []
        for sql, needle in (
            ("UPDATE public.zz_s6_support SET lad24cd = 'E08000038' WHERE "
             "lad24cd = 'E08000016'", "Barnsley/Sheffield"),
            ("UPDATE public.zz_s6_support SET lad24cd = 'E06999999' WHERE "
             "lad24cd = 'E06000001'", "not in la_boundaries"),
            ("UPDATE public.zz_s6_groups SET lad24cd = 'E08000039' WHERE "
             "lad24cd = 'E08000016'", "Barnsley/Sheffield"),
            ("UPDATE public.zz_s6_non_england SET lad_code = 'E06000001' "
             "WHERE lad_code = 'S12000033'", "outside S12/W06/N09"),
            ("UPDATE public.zz_s6_non_england SET country = 'Wales' WHERE "
             "lad_code = 'S12000033'", "country does not follow"),
        ):
            e.cur.execute("SAVEPOINT plant")
            try:
                e.cur.execute(sql)
                ok, detail = real_codes(e.cur, ZZ)
            except psycopg2.Error as ex:
                ok, detail = False, str(ex)
            e.cur.execute("ROLLBACK TO SAVEPOINT plant")
            e.cur.execute("RELEASE SAVEPOINT plant")
            if ok or needle not in detail:
                miss.append(f"{needle!r}: {detail[:90]}")
        out["miss"] = miss
        # a code nobody can explain, and both forms of Barnsley in one
        # period, halt and store nothing
        for label, add, needle in (
            ("unexplained", [[dtext(P_NEW), "Section 95", "North East",
                              "Nowhere", "E06999999",
                              "Dispersal Accommodation", 4]], "E06999999"),
            ("both forms", [[dtext(P_REV), "Section 95",
                             "Yorkshire and The Humber", "Barnsley",
                             "E08000016", "Dispersal Accommodation", 3]],
             "E080000")):
            e.d11(Q4, add=add)
            before = state(e.cur)
            ok, text = e.halts(["load", "--only", "d11", "--commit"], before,
                               needle)
            out[label] = ok
    scenario(cur, body)
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "x.py"
        p.write_text(
            '"""E08000038 in a docstring; la_code_lookup too"""\n'
            'import geography\n'
            'X = {"a": "E08000038"}\n'
            'Q = "SELECT old_code FROM la_code_lookup"\n'
            'OK = ("SELECT old_code, new_code FROM la_code_lookup WHERE "\n'
            '      "change_type = \'new_unitary\'")\n'
            'def f(v):\n    return v or 0\n'
            'def g(v):\n    return v or 0  # not a source value\n'
            'C = "SELECT COALESCE(x, 0) FROM t"\n', encoding="utf-8")
        ast_bad = _source_problems(p)
    ast_ok = (len(ast_bad) == 5
              and any("line 3:" in b for b in ast_bad)
              and any("line 4:" in b for b in ast_bad)
              and any("line 8:" in b for b in ast_bad)
              and any("line 11:" in b for b in ast_bad)
              and any("no call of geography.resolve" in b for b in ast_bad))
    ok = (out["base"][0] and out.get("barnsley") and not out["miss"]
          and ast_ok and out.get("unexplained") and out.get("both forms"))
    detail = ("Barnsley's E08000038 is stored as E08000016; five planted "
              "breaks (a new code in lad24cd, a code outside la_boundaries "
              "in support and in groups, a non-England code outside "
              "S12/W06/N09, a wrong country) are each caught; a file code "
              "nobody can explain and a period carrying both forms of "
              "Barnsley halt and store nothing; the AST check catches a "
              "private dict, a la_code_lookup query beyond new_unitary, a "
              "missing geography.resolve call and a value coerced to 0, and "
              "ignores a docstring, the declared read and a marked counter"
              if ok else f"seeded: {out} ast {ast_bad}")
    mixed(4, name, ok, detail, cur, real_codes)


def gate_2_edition1_and_latest(cur):
    name = ("every live period has edition 1; the latest edition equals "
            "live cell for cell (load_checks.check_latest_equals_live)")
    out = {}

    def body(e):
        out["base"] = real_edition1_and_latest(e.cur, ZZ)
        miss = []
        for sql, needle in (
            ("UPDATE public.zz_s6_support SET people = people + 1 WHERE "
             "lad24cd = 'E06000001' AND support_type = 'Section 95'",
             "edition-only"),
            ("UPDATE public.zz_s6_support SET published_la_name = 'x' WHERE "
             "lad24cd = 'E06000001'", "edition-only"),
            ("UPDATE public.zz_s6_support SET source_marker = 'x' WHERE "
             "lad24cd = 'E06000001'", "edition-only"),
            ("DELETE FROM public.zz_s6_support WHERE lad24cd = 'E06000001' "
             "AND support_type = 'Section 95'", "edition-only"),
            ("UPDATE public.zz_s6_unallocated SET people = people + 1",
             "edition-only"),
            ("UPDATE public.zz_s6_non_england SET country = 'Wales' WHERE "
             "lad_code = 'S12000033'", "edition-only"),
            ("UPDATE public.zz_s6_groups SET people = 99 WHERE "
             "suppressed", "edition-only"),
            ("UPDATE public.zz_s6_groups SET percentage_of_population = "
             "percentage_of_population + 0.0001 WHERE pathway = "
             "'all_pathways' AND lad24cd = 'E06000001'", "edition-only"),
            ("UPDATE public.zz_s6_support SET period_ending = "
             "'2030-03-31' WHERE period_ending = '2026-03-31'",
             "no edition 1"),
        ):
            e.cur.execute("SAVEPOINT plant")
            try:
                e.cur.execute(sql)
                ok, detail = real_edition1_and_latest(e.cur, ZZ)
            except (psycopg2.Error, ValueError, LookupError) as ex:
                ok, detail = False, str(ex)
            e.cur.execute("ROLLBACK TO SAVEPOINT plant")
            e.cur.execute("RELEASE SAVEPOINT plant")
            if ok or needle not in detail:
                miss.append(f"{sql[:50]!r}: {detail[:80]}")
        out["miss"] = miss
    scenario(cur, body)
    ok = out["base"][0] and not out["miss"]
    mixed(2, name, ok, "seeded complete state passes; nine planted drifts (a "
          "count, a name, a marker, a missing row, an unallocated count, a "
          "country, a Reg_02 count and a ratio, a period without edition 1) "
          "are each caught" if ok else f"{out['base']} {out['miss']}", cur,
          real_edition1_and_latest)


def gate_3_provenance(cur):
    name = ("live source_edition uniform per period and equal to the tip's "
            "release_label")
    out = {}

    def body(e):
        out["base"] = real_provenance(e.cur, ZZ)
        miss = []
        for sql, needle in (
            ("UPDATE public.zz_s6_support SET source_edition = 'other' "
             "WHERE lad24cd = 'E06000001' AND period_ending = '2026-03-31' "
             "AND support_type = 'Section 95'", "not uniform"),
            ("UPDATE public.zz_s6_support SET source_edition = 'other'",
             "differs from the tip"),
            ("UPDATE public.zz_s6_unallocated SET source_edition = 'x'",
             "differs from the tip"),
            ("UPDATE public.zz_s6_non_england SET source_edition = 'x' "
             "WHERE lad_code = 'S12000033' AND period_ending = "
             "'2026-03-31'", "not uniform"),
            ("UPDATE public.zz_s6_groups SET source_edition = 'year ending "
             "June 2026'", "differs from the tip"),
        ):
            e.cur.execute("SAVEPOINT plant")
            e.cur.execute(sql)
            ok, detail = real_provenance(e.cur, ZZ)
            e.cur.execute("ROLLBACK TO SAVEPOINT plant")
            e.cur.execute("RELEASE SAVEPOINT plant")
            if ok or needle not in detail:
                miss.append(f"{needle!r}: {detail[:80]}")
        out["miss"] = miss
        # a tip whose source_file names no rank
        e.cur.execute("SAVEPOINT plant")
        bad_tab = ED["support"]
        e.cur.execute(f"ALTER TABLE public.{bad_tab} DISABLE TRIGGER USER")
        e.cur.execute(f"UPDATE public.{bad_tab} SET source_file = 'x'")
        ok, detail = real_provenance(e.cur, ZZ)
        e.cur.execute("ROLLBACK TO SAVEPOINT plant")
        e.cur.execute("RELEASE SAVEPOINT plant")
        out["norank"] = (not ok) and "names no year-ending" in detail
    scenario(cur, body)
    ok = out["base"][0] and not out["miss"] and out.get("norank")
    mixed(3, name, ok, "seeded provenance passes; a non-uniform period, a "
          "uniform label that is not the tip's (three tables) and a tip "
          "without a file rank are each caught" if ok else
          f"{out['base']} {out['miss']} {out.get('norank')}", cur,
          real_provenance)


# ---------------------------------------------------------------- gate 5

def real_rule1(cur, specs=REAL):
    """Rule 1 on the stored rows (live and every edition): people never NULL
    or 0 in the three Asy_D11 tables; in groups NULL people iff suppressed
    iff source_marker '*' (a published 0 stays 0, never suppressed)."""
    bad, parts = [], []
    for tag, spec in specs.items():
        _need(cur, spec)
    for tag in D11:
        spec = specs[tag]
        for table in (spec.live_table, spec.editions_table):
            cur.execute(f"SELECT COUNT(*) FILTER (WHERE people IS NULL), "
                        f"COUNT(*) FILTER (WHERE people = 0), COUNT(*) FROM "
                        f"public.{table}")
            nulls, zeros, n = cur.fetchone()
            if nulls or zeros:
                bad.append(f"{table}: {nulls} NULL and {zeros} zero people")
            if not n:
                bad.append(f"{table}: no rows (an empty table is not a pass)")
    spec = specs["groups"]
    for table in (spec.live_table, spec.editions_table):
        cur.execute(f"""SELECT
            COUNT(*) FILTER (WHERE people IS NULL AND NOT suppressed),
            COUNT(*) FILTER (WHERE people IS NOT NULL AND suppressed),
            COUNT(*) FILTER (WHERE suppressed AND
                             source_marker IS DISTINCT FROM '*'),
            COUNT(*) FILTER (WHERE NOT suppressed AND
                             source_marker IS NOT DISTINCT FROM '*'),
            COUNT(*) FILTER (WHERE suppressed AND people = 0),
            COUNT(*) FILTER (WHERE suppressed),
            COUNT(*) FILTER (WHERE people = 0), COUNT(*)
            FROM public.{table}""")
        a, b, c, d, z, star, zeros, n = cur.fetchone()
        if a or b or c or d or z:
            bad.append(f"{table}: {a} NULL people not suppressed, {b} "
                       f"suppressed with people, {c} suppressed without '*', "
                       f"{d} '*' not suppressed, {z} suppressed zero")
        if not n:
            bad.append(f"{table}: no rows (an empty table is not a pass)")
        parts.append(f"{table} {star:,} '*' NULL, {zeros:,} published zero")
    return not bad, "; ".join(bad[:3]) if bad else (
        "no NULL or 0 people in the three Asy_D11 tables (live and "
        "editions); groups: NULL people iff suppressed iff '*' (" +
        "; ".join(parts) + ")")


def gate_5_rule_1(cur):
    name = ("rule 1: no NULL or 0 people in the three Asy_D11 tables; groups "
            "NULL people iff suppressed iff '*', no suppressed 0; a blank, "
            "text or zero cell halts; no source value coerced to 0")
    out = {}

    def body(e):
        out["base"] = real_rule1(e.cur, ZZ)
        e.cur.execute("SELECT COUNT(*) FILTER (WHERE people IS NULL AND "
                      "source_marker = '*'), COUNT(*) FILTER (WHERE people = "
                      "0) FROM public.zz_s6_groups")
        out["groups"] = e.cur.fetchone()
        miss = []
        for sql, needle in (
            ("UPDATE public.zz_s6_groups SET suppressed = false WHERE "
             "suppressed", "NULL people not suppressed"),
            ("UPDATE public.zz_s6_groups SET people = 3 WHERE suppressed",
             "suppressed with people"),
            ("UPDATE public.zz_s6_groups SET source_marker = NULL WHERE "
             "suppressed", "suppressed without"),
            ("UPDATE public.zz_s6_groups SET people = 0 WHERE suppressed",
             "suppressed zero"),
            ("UPDATE public.zz_s6_groups SET source_marker = '*' WHERE NOT "
             "suppressed", "'*' not suppressed"),
        ):
            e.cur.execute("SAVEPOINT plant")
            e.cur.execute(sql)
            ok, detail = real_rule1(e.cur, ZZ)
            e.cur.execute("ROLLBACK TO SAVEPOINT plant")
            e.cur.execute("RELEASE SAVEPOINT plant")
            if ok or needle not in detail:
                miss.append(f"{needle!r}: {detail[:80]}")
        # the three tables refuse a NULL people; a zero is caught by the gate
        for t in D11:
            e.cur.execute("SAVEPOINT plant")
            try:
                e.cur.execute(f"UPDATE public.zz_s6_{t} SET people = NULL")
                miss.append(f"zz_s6_{t} accepted a NULL people")
            except psycopg2.Error:
                pass
            e.cur.execute("ROLLBACK TO SAVEPOINT plant")
            e.cur.execute("RELEASE SAVEPOINT plant")
            e.cur.execute("SAVEPOINT plant")
            e.cur.execute(f"UPDATE public.zz_s6_{t} SET people = 0")
            ok, detail = real_rule1(e.cur, ZZ)
            e.cur.execute("ROLLBACK TO SAVEPOINT plant")
            e.cur.execute("RELEASE SAVEPOINT plant")
            if ok or "zero people" not in detail:
                miss.append(f"a zero in {t} was not caught: {detail[:60]}")
        out["miss"] = miss
        # a blank, a zero and text in an Asy_D11 file, a blank, '-' and text
        # in Reg_02, each halt and store nothing
        key = (P_NEW, "E06000001", "Section 95")
        for what, cell in (("blank", None), ("zero", 0), ("text", "[x]"),
                           ("negative", -1), ("fraction", 2.5)):
            e.d11(Q4, values={key: cell})
            ok, _ = e.halts(["load", "--only", "d11", "--commit"],
                            state(e.cur))
            out.setdefault("d11 cells", {})[what] = ok
        for what, cell in (("blank", None), ("dash", "-"), ("text", "n/a"),
                           ("negative", -1), ("fraction", 1.5)):
            e.reg("2026-03-31", values={"E06000001": {3: cell}},
                  published=date(2026, 9, 1))
            ok, _ = e.halts(["load", "--only", "reg02", "--commit"],
                            state(e.cur))
            out.setdefault("reg02 cells", {})[what] = ok
        # the cell readers
        def raises(fn, *a):
            try:
                fn(*a)
                return False
            except ValueError:
                return True
        out["readers"] = (
            raises(m.people_cell, None, "x") and raises(m.people_cell, 0, "x")
            and raises(m.people_cell, "", "x")
            and m.people_cell(5, "x") == 5
            and raises(m.reg02_cell, None, "x")
            and raises(m.reg02_cell, "-", "x")
            and raises(m.reg02_cell, "", "x")
            and m.reg02_cell("*", "x") == (None, "*")
            and m.reg02_cell(0, "x") == (0, None)
            and m.reg02_cell(7, "x") == (7, None))
    scenario(cur, body)
    src = _source_problems(LOADER)
    cells = {**out.get("d11 cells", {}), **{f"reg02 {k}": v for k, v in
                                            out.get("reg02 cells", {}).items()}}
    ok = (out["base"][0] and not out["miss"] and all(cells.values())
          and len(cells) == 10 and out.get("readers") and not src
          and out["groups"][0] > 0 and out["groups"][1] > 0)
    detail = ("the seeded state passes with Reg_02 '*' NULLs and published "
              "zeros in groups; five planted groups breaks and a zero in "
              "each Asy_D11 table are caught, a NULL people is refused by "
              "the three Asy_D11 tables; a blank, zero, text, negative and "
              "fractional Asy_D11 cell and a blank, '-', text, negative and "
              "fractional Reg_02 cell each halt and store nothing; "
              "people_cell(None) and (0) raise, reg02_cell('*') is NULL "
              "with the marker and a published 0 stays 0; no source value "
              "is coerced to 0 in the loader" if ok else
              f"seeded: {out} {src[:2]}")
    mixed(5, name, ok, detail, cur, real_rule1)


# --------------------------------------------------------- file gates (6, 7)

def held_file(key, files=None, raw_dir=None):
    """The file on disk whose sha256 is the held file's, or None."""
    files = m.LEGACY_FILES if files is None else files
    raw = Path(raw_dir) if raw_dir is not None else m.RAW_DIR
    if key not in files or not raw.is_dir():
        return None
    for p in sorted(list(raw.glob("*.xlsx")) + list(raw.glob("*.ods"))):
        if m.content_sha256(p) == files[key]["sha256"]:
            return p
    return None


def read_held(cur, files=None, raw_dir=None, which=("d11", "d09", "reg02_mar",
                                                    "reg02_jun")):
    """({key: path}, problems): the held files found by sha256."""
    paths, bad = {}, []
    for k in which:
        p = held_file(k, files, raw_dir)
        if p is None:
            bad.append(f"no file in {raw_dir or m.RAW_DIR} for the held {k} "
                       "(sha256 "
                       f"{(files or m.LEGACY_FILES)[k]['sha256'][:16]})")
        else:
            paths[k] = p
    return paths, bad


def build_held(cur, paths):
    """(d11 file, by_period, [(reg02 file, period, records)]) of the held
    files read now with the real code resolution (read-only)."""
    f = m.read_d11(paths["d11"])
    rmap, problems = m.resolve_codes(cur, set(), m.d11_codes_by_period(f))
    if problems:
        raise ValueError("geography: " + "; ".join(problems[:3]))
    by, _, _ = m.build_d11_records(f, rmap)
    regs = []
    for k in ("reg02_mar", "reg02_jun"):
        if k not in paths:
            continue
        g = m.read_reg02(paths[k])
        rmap, problems = m.resolve_codes(cur, set(), {
            g["period"].isoformat(): {r["code"] for r in g["rows"]
                                      if r["code"][:3] in m.ENGLISH_PREFIXES}})
        if problems:
            raise ValueError("geography: " + "; ".join(problems[:3]))
        p, recs = m.build_reg02_records(g, rmap)
        regs.append((k, g, p, recs))
    return f, by, regs


def real_reconciliation(cur, files=None, raw_dir=None, n_las=REAL_LAS):
    """The reconciliations re-run read-only from the held files on disk:
    the pivot cache (the latest eight quarters), England + non-England +
    unallocated = the data total in every period, Asy_D09 (England and
    unallocated), and each Reg_02 internally and against Asy_D11 per
    authority."""
    paths, bad = read_held(cur, files, raw_dir)
    if bad:
        return False, "; ".join(bad[:2])
    with las(n_las):
        f, by, regs = build_held(cur, paths)
        probs = m.reconcile_d11(f, by)
        probs += m.reconcile_d09(m.read_d09(paths["d09"]), by, f["cover"])
        tot = {p: m.d11_la_totals(t["support"]) for p, t in by.items()}
        for k, g, p, recs in regs:
            probs += [f"{paths[k].name}: {x}" for x in
                      m.reconcile_reg02(g, recs, tot.get(p))]
    if probs:
        return False, f"reconciliation failed: {probs[0]}"
    last = max(by)
    eng = sum(r["people"] for r in by[last]["support"])
    return True, (f"{paths['d11'].name}: pivot cache of {len(f['pivot']['dates'])} "
                  f"quarter(s) equals the data sheet, {len(by)} periods "
                  "England + non-England + unallocated = the data total, "
                  f"Asy_D09 agrees, {len(regs)} Reg_02 file(s) reconcile "
                  f"internally and against Asy_D11; England at {last} {eng:,}")


def gate_6_reconciliations(cur):
    name = ("reconciliations re-run read-only from the files on disk (pivot "
            "cache, Asy_D09, per-period UK total, Reg_02 internal and "
            "against Asy_D11); a planted cell off by one is caught")
    problems = []

    def resolver(c):
        return {"E08000038": "E08000016"}.get(c, c)

    def body(e):
        with las(4):
            d = e.root / "disk"
            rows = d11_rows(Q4)
            p11 = write_d11(d / "d11.xlsx", Q4, rows=rows)
            p09 = write_d09(d / "d09.xlsx", Q4, rows=rows)
            rm = write_reg02(d / "rm.ods", "2026-03-31", d11=rows)
            rj = write_reg02(d / "rj.ods", "2026-06-30", d11=rows)
            files = {k: {"sha256": m.content_sha256(p)} for k, p in (
                ("d11", p11), ("d09", p09), ("reg02_mar", rm),
                ("reg02_jun", rj))}
            ok, detail = real_reconciliation(e.cur, files, d, 4)
            if not ok:
                problems.append(f"the clean fixtures do not reconcile: "
                                f"{detail}")
            ok, detail = real_reconciliation(
                e.cur, {**files, "d09": {"sha256": "0" * 64}}, d, 4)
            if ok or "no file" not in detail:
                problems.append("a missing file was accepted")
            # each planted off-by-one, as a file on disk
            def planted(label, needle, **kw):
                dd = e.root / label
                f2 = dict(files)
                kw11 = {k: v for k, v in kw.items() if k == "pivot"}
                if "pivot" in kw:
                    pp = write_d11(dd / "d11.xlsx", Q4, rows=rows, **kw11)
                    f2["d11"] = {"sha256": m.content_sha256(pp)}
                if "d09" in kw:
                    pp = write_d09(dd / "d09.xlsx", Q4, rows=rows,
                                   values=kw["d09"])
                    f2["d09"] = {"sha256": m.content_sha256(pp)}
                if "reg" in kw:
                    pp = write_reg02(dd / "rj.ods", "2026-06-30", d11=rows,
                                     values=kw["reg"])
                    f2["reg02_jun"] = {"sha256": m.content_sha256(pp)}
                ok, detail = real_reconciliation(e.cur, f2, dd, 4)
                if ok or needle not in detail:
                    problems.append(f"{label}: {detail[:120]}")
            # (the directory holds only the planted file; the others are
            # copied beside it so every held file is found by sha256)
            for label in ("pivot", "d09", "reg_total", "reg_sum"):
                dd = e.root / label
                dd.mkdir(parents=True, exist_ok=True)
                for p in (p11, p09, rm, rj):
                    (dd / p.name).write_bytes(p.read_bytes())
            planted("pivot", "the pivot says 323",
                    pivot={(dtext(P_MAR), "North East"): 323})
            planted("d09", "2025-12-31: England",
                    d09={(dtext(P_REV), "North East"): 1})
            planted("reg_total", "Reg_02 supported_asylum 999",
                    reg={"E06000001": {5: 999, 7: 999}})
            planted("reg_sum", "its parts sum to",
                    reg={"E06000001": {2: 5}})
            # England + non-England + unallocated = the data total
            f = m.read_d11(p11)
            by = m.build_d11_records(f, resolver)[0]
            by[P_REV]["support"] = by[P_REV]["support"][1:]
            out = m.reconcile_d11(f, by)
            if not any(f"{P_REV}: England + non-England" in x for x in out):
                problems.append(f"a dropped England row was not caught: "
                                f"{out[:1]}")
            # the real counts: not 296 authorities
            ok, detail = real_reconciliation(e.cur, files, d, 296)
            if ok or "expected 296" not in detail:
                problems.append(f"4 authorities passed as 296: {detail[:80]}")
    scenario(cur, body, seed=False)
    seeded = not problems
    sd = ("seeded: clean fixtures reconcile and are found by sha256; a "
          "pivot cell, an Asy_D09 region, a Reg_02 supported_asylum against "
          "Asy_D11, a Reg_02 parts sum, a missing England row (UK total) "
          "and a wrong authority count are each caught" if seeded
          else "; ".join(problems[:3]))
    if not seeded:
        return report(6, name, False, sd)
    try:
        ok, detail = real_reconciliation(cur)
    except (psycopg2.Error, SystemExit, ValueError, LookupError) as ex:
        return report(6, name, False, _first(ex))
    report(6, name, ok, f"{sd}; held files: {detail}")


def real_identity(cur=None, files=None, raw_dir=None, specs=REAL):
    """Each held file's identity read from the file itself (cover, Contents,
    header, title) equals what the loader surveyed (kind, year-ending and
    published date), and, once the editions tables exist, every period's tip
    names its file's rank."""
    files = m.LEGACY_FILES if files is None else files
    paths, bad = read_held(cur, files, raw_dir)
    if bad:
        return False, "; ".join(bad[:2])
    kinds = {"d11": "d11", "d09": "d09", "reg02_mar": "reg02",
             "reg02_jun": "reg02"}
    parts = []
    for k, p in paths.items():
        try:
            f = m.read_any(p)
        except ValueError as e:
            bad.append(f"{p.name}: {_first(e)}")
            continue
        cov = f["cover"]
        if cov["kind"] != kinds[k]:
            bad.append(f"{p.name}: the cover says {cov['kind']}, expected "
                       f"{kinds[k]}")
        if tuple(cov["rank"]) != tuple(files[k]["rank"]):
            bad.append(f"{p.name}: rank {cov['rank']}, surveyed "
                       f"{files[k]['rank']}")
        parts.append(f"{p.name} {cov['label']}, published {cov['published']}")
    tips = ""
    if cur is not None and exists(cur, specs):
        n = 0
        for spec in specs.values():
            info = m.tip_info(cur, spec)
            off = [p for p, i in info.items() if i["rank"] is None]
            if off:
                bad.append(f"{spec.live_table} {off[:3]}: the tip's "
                           "source_file names no file rank")
            n += len(info)
        tips = f"; {n} stored table-period(s), each tip names its file"
    return not bad, "; ".join(bad[:3]) if bad else (
        "; ".join(parts) + "; covers, Contents, headers and titles agree"
        + tips)


def gate_7_identity(cur):
    name = ("identity from the file: a seeded cover, title, Contents or "
            "header mismatch halts")
    problems = []

    def body(e):
        tmpd = e.root

        def raises(label, fn, path_fn):
            try:
                fn(path_fn())
                problems.append(f"{label} was read")
            except ValueError:
                pass

        def d11(label, **kw):
            raises(label, m.read_d11, lambda: write_d11(
                tmpd / f"{label}.xlsx", Q3, **kw))

        def reg(label, **kw):
            raises(label, m.read_reg02, lambda: write_reg02(
                tmpd / f"{label}.ods", "2026-06-30", **kw))
        from test_s6_asylum_pure import cover_rows
        d11("cover_title", cover=cover_rows("d09", Q3[-1]))
        d11("cover_series", cover=[["Other series"]] + cover_rows(
            "d11", Q3[-1])[1:])
        d11("cover_label", cover=cover_rows("d11", Q3[-1],
                                            label="Q1 2026"))
        d11("cover_no_published", cover=[r for r in cover_rows("d11", Q3[-1])
                                         if not r[0].startswith("Published")])
        d11("contents", contents_period="2014 to 2024 Q4")
        d11("header", header=["x"] + list(m.D11_HEADERS[1:]))
        d11("pivot_filter", pivot_filter="Section 95")
        d11("latest_date", cover=cover_rows("d11", "2026-06-30"))
        raises("d09_as_d11", m.read_d11, lambda: write_d09(
            tmpd / "d09.xlsx", Q3))
        reg("reg_cover", cover=cover_rows("d11", "2026-06-30"))
        reg("reg_title", title="Reg_02: Immigration groups, by Local "
            "Authority, as at 31 March 2026")
        reg("reg_title_text", title="Immigration groups")
        reg("reg_header", header=["x"] + list(m.REG02_HEADERS[1:]))
        # through the command: a mismatching file halts and stores nothing
        before = state(e.cur)
        path = write_d11(tmpd / "x" / "c.xlsx", Q3,
                         cover=cover_rows("d11", "2026-06-30"))
        ok, _ = e.halts(["load", "--file", path, "--no-page", "--no-d09",
                         "--commit"], before)
        if not ok:
            problems.append("--file with a wrong cover did not halt")
        # a Reg_02 file given to --only d11
        rp = e.reg("2026-06-30", register=False)
        ok, _ = e.halts(["load", "--only", "d11", "--file", rp, "--no-page",
                         "--commit"], before, "cover")
        if not ok:
            problems.append("--only d11 with a Reg_02 file did not halt")
        # a quiet fixture reads
        m.read_d11(write_d11(tmpd / "ok.xlsx", Q3))
        m.read_reg02(write_reg02(tmpd / "ok.ods", "2026-06-30"))
    scenario(cur, body, seed=False)
    seeded = not problems
    sd = ("seeded: a wrong publication title, series line, year-ending "
          "label, missing Published line, Contents period, data header, "
          "pivot filter, latest date, an Asy_D09 given as Asy_D11, and a "
          "Reg_02 cover, title date, title text or header that disagrees "
          "with the file each halt, and nothing is stored" if seeded
          else "; ".join(problems[:3]))
    if not seeded:
        return report(7, name, False, sd)
    try:
        ok, detail = real_identity(cur if exists(cur) else None)
    except (psycopg2.Error, SystemExit, ValueError, LookupError) as ex:
        return report(7, name, False, _first(ex))
    report(7, name, ok, f"{sd}; held files: {detail}")


# ---------------------------------------------------------------- gate 8-9

def gate_8_older_file(cur):
    name = ("older-file guard on the page, --release and --file paths "
            "(Asy_D11 and Reg_02); the rank is the file's own, never its "
            "name; equal rank with other content stops")
    problems = []
    rev = {(P_REV, "E06000001", "Section 95"): 104}

    def d11_argv(e, how):
        # held: the June release. Offered: March content (older) with a name
        # that looks newest
        e.d11(Q4)
        e.ok(e.cur, ["load", "--only", "d11", "--commit"])
        p11, p09 = e.d11(Q3, register=how != "file", tag="sep-2026",
                         values=rev)
        argv = ["load", "--only", "d11", "--commit"]
        if how == "release":
            argv += ["--release", "March 2026"]
        if how == "file":
            argv += ["--file", p11, "--d09-file", p09]
        return argv

    def reg_argv(e, how):
        e.d11(Q4)
        e.ok(e.cur, ["load", "--only", "d11", "--commit"])
        e.reg("2026-06-30")
        e.ok(e.cur, ["load", "--only", "reg02", "--commit"])
        p = e.reg("2026-06-30", register=how != "file", tag="sep-2026",
                  published=date(2026, 8, 1), values={"E06000001": {0: 11}},
                  url="https://assets.example/media/o/sep-2026.ods")
        argv = ["load", "--only", "reg02", "--commit"]
        if how == "release":
            argv += ["--release", "June 2026"]
        if how == "file":
            argv += ["--file", p]
        return argv

    for what, mk in (("Asy_D11", d11_argv), ("Reg_02", reg_argv)):
        for how in ("page", "release", "file"):
            def one(c, what=what, mk=mk, how=how):
                with env(c) as e, tl.specs():
                    argv = mk(e, how)
                    before = state(c)
                    rc, text, logged = e.cmd(argv)
                    if not (rc == "halt" and "older file" in text
                            and state(c) == before and not logged.called):
                        problems.append(f"{what} {how}: rc {rc}: "
                                        f"{text[-150:]}")
            _in_savepoint(cur, one)

    def override(c):
        with env(c) as e, tl.specs():
            argv = d11_argv(e, "file") + ["--allow-older-file"]
            rc, text, logged = e.cmd(argv)
            eds = tl.editions(c, "support", P_REV)
            if not (rc == 0 and eds == [(1, None, 5), (2, 1, 5)]
                    and logged.called and "--allow-older-file given"
                    in logged.call_args[0][2]):
                problems.append(f"--allow-older-file: rc {rc}, {eds}")
    _in_savepoint(cur, override)

    def equal_rank(c):
        with env(c) as e, tl.specs():
            e.seed_all()
            # Asy_D11: the same cover, other content
            e.d11(Q3, values=rev, url="https://assets.example/media/z/x.xlsx")
            before = state(c)
            rc, text, logged = e.cmd(["load", "--only", "d11", "--commit"])
            if not (rc == 1 and "two files claim the same release" in text
                    and f"--accept-reissue {P_REV}" in text
                    and logged.called
                    and f"rejected {P_REV}" in logged.call_args[0][2]
                    and tl.editions(c, "support", P_REV) == [(1, None, 5)]):
                problems.append(f"Asy_D11 equal rank: rc {rc}: {text[-120:]}")
            rc, text, logged = e.cmd(["load", "--only", "d11", "--commit",
                                      "--accept-reissue", P_REV])
            if not (rc == 0 and "ACCEPTED REISSUE" in text
                    and tl.editions(c, "support", P_REV) ==
                    [(1, None, 5), (2, 1, 5)]
                    and f"--accept-reissue {P_REV}"
                    in logged.call_args[0][2]):
                problems.append(f"Asy_D11 --accept-reissue: rc {rc}: "
                                f"{text[-120:]}")

    _in_savepoint(cur, equal_rank)

    def equal_rank_reg(c):
        with env(c) as e, tl.specs():
            e.seed_all()
            e.reg("2026-03-31", values={"E06000001": {0: 11}},
                  url="https://assets.example/media/new/reissue.ods")
            rc, text, logged = e.cmd(["load", "--only", "reg02", "--commit"])
            if not (rc == 1 and "two files claim the same release" in text
                    and tl.editions(c, "groups", P_MAR) == [(1, None, 48)]):
                problems.append(f"Reg_02 equal rank: rc {rc}: {text[-120:]}")
    _in_savepoint(cur, equal_rank_reg)
    report(8, name, not problems, "a March-content file named like "
           "September 2026, offered by the page, --release and --file "
           "against a held June release (and an August Reg_02 against a "
           "held 27-day-later one), is skipped and halts with nothing stored "
           "or logged; --allow-older-file overrides and is logged; equal "
           "rank with other content is REJECTED without --accept-reissue "
           "and stored with it" if not problems
           else "; ".join(problems[:3]))


def rejects(e, argv, needle, period, *, commit=True, acknowledge_ok=True):
    """None if the command was REJECTED (exit 1) naming `needle`, stored
    nothing for `period` (no editions, live or ledger rows), and (with
    --commit) wrote one partial run-log row; else the problem."""
    before = period_state(e.cur, period)
    rc, text, logged = e.cmd(argv)
    if rc != 1 or "REJECTED" not in text or needle not in text:
        return f"rc {rc}, not REJECTED naming {needle!r}: {text[-160:]}"
    if period_state(e.cur, period) != before:
        return f"{period}: something was stored or a ledger row written"
    if commit:
        if logged.call_count != 1:
            return "no partial run-log row"
        notes = logged.call_args[0][2]
        if "PARTIAL RUN (exit 1)" not in notes or \
                f"rejected {period}" not in notes:
            return f"run-log notes: {notes[:120]}"
    elif logged.called:
        return "a preview wrote a run-log row"
    return None


def gate_9_stop_conditions(cur):
    name = ("stop conditions: each seeded REJECTED, stores nothing for the "
            "period, no ledger row, partial run-log row")
    problems = []
    d11_argv = ["load", "--only", "d11", "--commit"]
    rg_argv = ["load", "--only", "reg02", "--commit"]

    def d11_case(period, needle, quarters=Q4, extra=(), argv=d11_argv,
                 commit=True, **kw):
        def fn(e):
            e.d11(quarters, **kw)
            return rejects(e, list(argv) + list(extra), needle, period,
                           commit=commit)
        return fn

    def reg_case(needle, **kw):
        def fn(e):
            e.reg("2026-03-31", published=date(2026, 9, 1), **kw)
            return rejects(e, rg_argv, needle, P_MAR)
        return fn

    def partial_not_released(e):
        e.d11(Q4, drop=[(P_REV, "E06000001", "Section 95")])
        return rejects(e, d11_argv + ["--acknowledge", P_REV], "PARTIAL FILE",
                       P_REV)

    def acknowledged(e):
        e.d11(Q4, values={(P_NEW, "E06000001", "Section 95"): 2000})
        rc, text, logged = e.cmd(d11_argv + ["--acknowledge", P_NEW])
        if not (rc == 0 and "ACKNOWLEDGED" in text and logged.called
                and f"ACKNOWLEDGED {P_NEW}" in logged.call_args[0][2]
                and tl.editions(e.cur, "support", P_NEW) == [(1, None, 5)]):
            return f"a named acknowledgement did not store: rc {rc}"
        return None

    def within(e):
        e.d11(Q4, values={(P_REV, "E06000001", "Section 95"): 102})
        rc, text, _ = e.cmd(d11_argv)
        if rc != 0 or tl.editions(e.cur, "support", P_REV) != [
                (1, None, 5), (2, 1, 5)]:
            return f"a revision within the limits was refused: rc {rc}"
        return None

    def partial_fn(e):
        spec = tl.ZZ["support"]
        tip = m.records(e.cur, spec, P_REV, 1)
        out = m.period_problems(tip[:-1], tip, None, table="support",
                                kind="revised")
        bad = None if any(m.PARTIAL in x for x in out) else \
            f"a short file: {out}"
        ok = [dict(r) for r in tip]
        ok[0]["people"] += 1
        if m.period_problems(ok, tip, None, table="support",
                             kind="revised"):
            bad = "a one-cell revision was rejected"
        g = m.records(e.cur, tl.ZZ["groups"], P_MAR, 1)
        out = m.period_problems(g[:-60], None, None, table="groups",
                                kind="new")
        if not any("English authorities" in x for x in out) and \
                len({r["lad24cd"] for r in g[:-60]}) != len(
                    {r["lad24cd"] for r in g}):
            bad = f"fewer Reg_02 authorities: {out}"
        return bad
    items = [
        ("new: one authority above 1,500 people",
         d11_case(P_NEW, "more than 1,500 people",
                  values={(P_NEW, "E06000001", "Section 95"): 2000})),
        ("new: England total +15%",
         d11_case(P_NEW, "against the previous period (limit 15%)",
                  values={(P_NEW, "E06000001", "Section 95"): 1000})),
        ("new: preview of the same writes no run-log row",
         d11_case(P_NEW, "more than 1,500 people", argv=["load", "--only",
                                                         "d11"], commit=False,
                  values={(P_NEW, "E06000001", "Section 95"): 2000})),
        ("revision: England total +2%",
         d11_case(P_REV, "(limit 2%)",
                  values={(P_REV, "E06000001", "Section 95"): 450})),
        ("revision: one authority +500 people",
         d11_case(P_REV, "more than 500 people",
                  values={(P_REV, "E06000001", "Section 95"): 700})),
        ("revision: a period's rows vanish (PARTIAL FILE)",
         d11_case(P_REV, "PARTIAL FILE",
                  drop=[(P_REV, "E06000001", "Section 95")])),
        ("revision: a held unallocated period missing from the file",
         d11_case(P_FIRST, "the file has no rows", unallocated=(),
                  extra=("--acknowledge", P_FIRST))),
        ("a partial file is never released by --acknowledge",
         partial_not_released),
        ("Reg_02: a 0 <-> '*' flip needs --acknowledge",
         reg_case("go from 0 to '*'", values={"E06000001": {2: "*"}})),
        ("Reg_02: England pathway total +5%",
         reg_case("England homes_for_ukraine total",
                  values={"E06000001": {0: 500}})),
        ("a named acknowledgement stores and is logged", acknowledged),
        ("a revision within the limits is stored", within),
        ("period_problems: a short file and a one-cell revision", partial_fn),
    ]
    scenario(cur, lambda e: problems.extend(cases(e, items)))
    report(9, name, not problems, "a new period with a large authority or "
           "England move, a revision with a large total or authority move or "
           "rows vanishing (not released by --acknowledge), a held "
           "unallocated period missing, and a Reg_02 0 <-> '*' flip or "
           "England pathway move, are each REJECTED (exit 1) with nothing "
           "stored for the period, no ledger row and one partial run-log row "
           "(none in a preview); a named acknowledgement stores and is "
           "logged; a revision within the limits is stored"
           if not problems else "; ".join(problems[:3]))


# --------------------------------------------------------------- gates 10-12

def gate_10_one_transaction(cur):
    name = ("a new period: editions, live rows and ledger rows of the three "
            "Asy_D11 tables in one transaction")
    problems = []

    def good(e):
        e.d11(Q3)
        e.reg("2026-03-31", register=True)
        before = state(e.cur)
        rc, text, logged = e.cmd(["load", "--only", "d11", "--commit"])
        for t, n, per in (("support", 5, [P_FIRST, P_REV, P_MAR]),
                          ("non_england", 2, [P_FIRST, P_REV, P_MAR]),
                          ("unallocated", 1, [P_FIRST])):
            for p in per:
                if tl.editions(e.cur, t, p) != [(1, None, n)] \
                        or tl.count(e.cur, f"zz_s6_{t}", "WHERE "
                                    "period_ending = %s", (p,)) != n:
                    problems.append(f"{t} {p}: edition 1 and {n} live rows "
                                    "not stored together")
                led = [(o, ed) for q, _, o, ed in tl.ledger(e.cur, t)
                       if q == p]
                if led != [("new", 1)]:
                    problems.append(f"{t} {p}: ledger {led}, expected one "
                                    "'new' row")
        if rc != 0 or state(e.cur) == before:
            problems.append(f"the load: rc {rc}")
        for t in D11:
            if not pe.status(e.cur, m.profile(t))["ok"]:
                problems.append(f"status not clean for {t}")
            bad = load_checks.check_latest_equals_live(e.cur, tl.ZZ[t])
            if bad:
                problems.append(f"live differs from edition 1: {bad[:1]}")
        # a quarter with no unallocated rows stores nothing there
        if tl.editions(e.cur, "unallocated", P_MAR):
            problems.append("an unallocated edition for a quarter with no "
                            "unallocated rows")
    scenario(cur, good, seed=False)

    def failing(label, target, attr, make):
        def body(c):
            with env(c) as e, tl.specs():
                e.d11(Q3)
                before = state(c)
                real = getattr(target, attr)
                with mock.patch.object(target, attr, side_effect=make(real)):
                    rc, text, logged = e.cmd(["load", "--only", "d11",
                                              "--commit"])
                if not (rc == 1 and state(c) == before
                        and f"{P_FIRST}: FAILED" in text and logged.called
                        and "PARTIAL RUN" in logged.call_args[0][2]
                        and f"failed {P_FIRST}" in logged.call_args[0][2]):
                    problems.append(f"{label}: rc {rc}, state kept="
                                    f"{state(c) == before}")
        _in_savepoint(cur, body)

    def boom_on(name_attr):
        def make(real):
            def f(c, a, *x, **kw):
                spec = getattr(a, "spec", a)
                if spec.name == "zz_s6_non_england":
                    raise RuntimeError("boom")
                return real(c, a, *x, **kw)
            return f
        return make
    failing("a failing non-England ledger insert", pe, "record_file_check",
            boom_on("spec"))
    failing("a failing non-England live insert", m, "insert_live",
            boom_on("spec"))
    failing("a failing non-England edition insert", core, "insert_edition",
            boom_on("spec"))
    report(10, name, not problems, "the support, non-England and unallocated "
           "editions, live rows and one 'new' ledger row each are stored "
           "together (a quarter without unallocated rows stores nothing "
           "there); a failing ledger insert, live insert or edition insert "
           "in the third table rolls back all three and logs a partial run"
           if not problems else "; ".join(problems[:3]))


def gate_11_ledger_skip(cur):
    name = ("ledger skip needs the (URL, sha256) pair for every period in "
            "every table")
    problems = []

    def body(e):
        p11, _ = e.d11(Q3)
        sha = m.content_sha256(p11)
        f = m.read_d11(p11)
        by = m.build_d11_records(f, lambda c: {"E08000038": "E08000016"}
                                 .get(c, c))[0]
        src = m.ledger_source(e.u11, f["cover"], {
            t: [p for p in sorted(by) if by[p][t]] for t in D11})
        for t in D11:
            for p in sorted(by):
                if not by[p][t] or (t == "non_england" and p == P_MAR):
                    continue
                e.cur.execute(f"INSERT INTO public.{LEDGER[t]} (period_ending,"
                              " source_file, file_sha256, outcome, edition) "
                              "VALUES (%s, %s, %s, 'new', 1)", (p, src, sha))
        rc, text, _ = e.cmd(["load", "--only", "d11"])
        if rc != 0 or "nothing parsed" in text or \
                f"{P_MAR}: support new" not in text:
            problems.append("a pair missing for one table's period skipped "
                            "the file")
        e.cur.execute(f"INSERT INTO public.{LEDGER['non_england']} "
                      "(period_ending, source_file, file_sha256, outcome, "
                      "edition) VALUES (%s, %s, %s, 'new', 1)",
                      (P_MAR, src, sha))
        rc, text, _ = e.cmd(["load", "--only", "d11"])
        if "nothing parsed" not in text:
            problems.append("the pair held for every period of every table "
                            "did not skip")
        # (URL, sha) held, but under another URL: read
        e.final[e.u11] = "https://assets.example/media/other/x.xlsx"
        rc, text, _ = e.cmd(["load", "--only", "d11"])
        if "nothing parsed" in text:
            problems.append("a pair held for another URL skipped the file")
        e.final.clear()
    scenario(cur, body, seed=False)

    def same_name(e):
        url0 = e.u11
        p0 = e.urls[url0]
        atts = [dict(a) for a in e.tables["details"]["attachments"]]
        e.d11(Q3, values={(P_REV, "E06000001", "Section 95"): 102},
              url=url0, published=date(2026, 5, 22))
        rc, text, _ = e.cmd(["load", "--only", "d11"])
        if "nothing parsed" in text or f"{P_REV}: support revised" not in text:
            problems.append("the same URL with other bytes was skipped")
        e.urls[url0] = p0
        e.tables["details"]["attachments"][:] = atts
        rc, text, _ = e.cmd(["load", "--only", "d11"])
        if "nothing parsed" not in text:
            problems.append("the held pair no longer skips")
        rc, text, _ = e.cmd(["load", "--only", "d11", "--recheck", P_MAR])
        if f"{P_MAR}: support unchanged" not in text or \
                "nothing parsed" in text:
            problems.append("--recheck did not read a ledger-held file")
    scenario(cur, lambda e: (e.d11(Q3), e.ok(e.cur, ["load", "--only", "d11",
                                                     "--commit"]),
                             same_name(e)), seed=False)
    report(11, name, not problems, "a pair missing for one table's period "
           "does not skip; the pair held for every period of every table "
           "skips with nothing parsed; the same URL with other bytes and a "
           "pair under another URL are read; --recheck reads a held file"
           if not problems else "; ".join(problems[:3]))


def gate_12_preview(cur):
    name = "preview and simulate write nothing"
    problems = []
    cur.execute("SELECT COUNT(*) FROM public.pipeline_run_log")
    runs = cur.fetchone()[0]

    def body(e):
        e.d11(Q3)
        e.reg("2026-03-31")
        before = state(e.cur)
        for argv in (["load"], ["load", "--simulate"], ["status"]):
            rc, text, logged = e.cmd(argv)
            if rc not in (0, 1) or state(e.cur) != before or logged.called:
                problems.append(f"new period {argv}: rc {rc}")
        e.ok(e.cur, ["load", "--commit"])
        e.d11(Q4, values={(P_REV, "E06000001", "Section 95"): 104})
        before = state(e.cur)
        for argv in (["load"], ["load", "--simulate"], ["status"]):
            rc, text, logged = e.cmd(argv)
            if rc not in (0, 1) or state(e.cur) != before or logged.called:
                problems.append(f"{argv}: rc {rc}")
        e.ok(e.cur, ["load", "--only", "d11", "--commit"])
        before = state(e.cur)
        for argv in (["refresh-latest"], ["refresh-latest", "--simulate"],
                     ["restore-edition", "zz_s6_support", P_REV, "1"],
                     ["restore-edition", "zz_s6_support", P_REV, "1",
                      "--simulate"]):
            rc, text, logged = e.cmd(argv)
            if rc != 0 or state(e.cur) != before or logged.called:
                problems.append(f"{argv} after a revision: rc {rc}")
    scenario(cur, body, seed=False)

    def legacy(e, paths, leg, files):
        before = state(e.cur)
        for extra in ((), ("--simulate",)):
            rc, text, logged = mig(e, paths, leg, files, extra)
            if rc != 0 or state(e.cur) != before or logged.called:
                problems.append(f"migrate-legacy {extra}: rc {rc}")
        # ddl
        rc, text, logged = e.cmd(["ddl"])
        if rc != 0 or state(e.cur) != before or logged.called:
            problems.append(f"ddl preview: rc {rc}")
    legacy_world(cur, legacy)
    cur.execute("SELECT COUNT(*) FROM public.pipeline_run_log")
    if cur.fetchone()[0] != runs:
        problems.append("the run log changed")
    report(12, name, not problems, "load, status, refresh-latest, "
           "restore-edition, ddl and migrate-legacy in preview and "
           "--simulate leave the editions, the live tables and the ledgers "
           "as they were and log nothing" if not problems
           else "; ".join(problems[:3]))


# ---------------------------------------------------------------- gate 13

def real_held_reread(cur, specs=REAL, files=None, raw_dir=None, n_las=REAL_LAS):
    """The held files read again equal edition 1 'as loaded' in every
    period they state: the content sha256 (key + compared columns, so Reg_02
    NULLs for '*' and published zeros included; provenance is not in it) of
    each table-period built from the file now equals edition 1's; edition
    1's source_file names the file's rank; and the ledgers hold the file's
    (URL, sha256) pair for every period of every table it feeds. So a
    re-read by the page, --release or --file is unchanged."""
    files = m.LEGACY_FILES if files is None else files
    for spec in specs.values():
        _need(cur, spec)
    paths, bad = read_held(cur, files, raw_dir, which=("d11", "reg02_mar",
                                                       "reg02_jun"))
    if bad:
        return False, "; ".join(bad[:2])
    with las(n_las):
        f, by, regs = build_held(cur, paths)
    checked = {t: m.ledger_checks(cur, m.dataclasses.replace(
        m.profile(t), spec=specs[t])) for t in TAGS}
    parts, stars, zeros = [], 0, 0
    work = [("d11", tag, p, by[p][tag], f["cover"]) for p in sorted(by)
            for tag in D11 if by[p][tag]]
    work += [(k, "groups", p, recs, g["cover"]) for k, g, p, recs in regs]
    for key, tag, p, recs, cov in work:
        spec = specs[tag]
        held = m.records(cur, spec, p, 1)
        if not held:
            bad.append(f"{spec.live_table} {p}: no edition 1")
            continue
        a, b = m.rows_content_sha(spec, held), m.rows_content_sha(spec, recs)
        if a != b:
            bad.append(f"{spec.live_table} {p}: edition 1 content sha "
                       f"{a[:12]} differs from the held file read now "
                       f"{b[:12]}")
        cur.execute(f"SELECT DISTINCT source_file FROM public."
                    f"{spec.editions_table} WHERE period_ending = %s AND "
                    "edition = 1", (p,))
        sfs = [r[0] for r in cur.fetchall()]
        if len(sfs) != 1 or not sfs[0].startswith("as loaded: ") or \
                m.release_rank(sfs[0]) != tuple(cov["rank"]):
            bad.append(f"{spec.live_table} {p}: edition 1 is not 'as loaded' "
                       f"from a file of rank {cov['rank']} ({sfs[:1]})")
        if tag == "groups":
            stars += sum(1 for r in recs if r["people"] is None)
            zeros += sum(1 for r in recs if r["people"] == 0)
    for key in ("d11", "reg02_mar", "reg02_jun"):
        sha = m.content_sha256(paths[key])
        url = files[key]["url"]
        got = m.ledger_complete({t: checked[t] for t in TAGS}, url, sha, {})
        need = sorted({p for k, t, p, _, _ in work if k == key})
        if got is None or not set(need) <= set(got):
            bad.append(f"the ledgers do not hold {paths[key].name}'s "
                       f"(URL, sha256) pair for every period it states "
                       f"({need[:3]}...)")
    parts = (f"{len(work)} table-period(s) of the held files read now equal "
             f"edition 1 as loaded ({stars:,} Reg_02 '*' NULL cells and "
             f"{zeros:,} published zeros included), and the ledgers hold "
             "each file's pair for every period")
    return not bad, "; ".join(bad[:3]) if bad else parts


def reread_paths(e, paths, files):
    """The same bytes by every path: the page, --recheck, --release, --file
    (with and without the page), with --commit. Problems."""
    p11, p09, rm, rj = paths
    out = []
    before = {t: (tl.count(e.cur, ED[t]), tl.count(e.cur, f"zz_s6_{t}"))
              for t in TAGS}
    e.urls[files["d11"]["url"]] = p11
    e.urls[e.URL + "d09.xlsx"] = p09
    e.tables["details"]["attachments"] = [
        {"title": "Asylum seekers in receipt of Home Office support by "
         "local authority detailed datasets, year ending June 2026",
         "url": files["d11"]["url"]},
        {"title": "Asylum seekers in receipt of Home Office support "
         "detailed datasets, year ending June 2026",
         "url": e.URL + "d09.xlsx"}]
    for k, path in (("reg02_mar", rm), ("reg02_jun", rj)):
        e.urls[files[k]["url"]] = path
    ye = {"reg02_mar": "March 2026", "reg02_jun": "June 2026"}
    e.regional["details"]["attachments"] = [
        {"title": "Regional and local authority data on immigration groups, "
         f"year ending {ye[k]}", "url": files[k]["url"]} for k in ye]
    rc, text, _ = e.cmd(["load"])
    if rc != 0 or text.count("nothing parsed") != 2:
        out.append(f"the page: rc {rc}, not skipped by the ledger")
    for argv in (["load", "--recheck", P_NEW],
                 ["load", "--only", "reg02", "--release", "March 2026",
                  "--recheck", P_MAR],
                 ["load", "--file", p11, "--d09-file", p09],
                 ["load", "--file", p11, "--no-page", "--no-d09"],
                 ["load", "--file", rm],
                 ["load", "--file", rj, "--no-page"],
                 ["load", "--file", p11, "--no-page", "--no-d09", "--commit"],
                 ["load", "--file", rj, "--no-page", "--commit"],
                 ["load", "--file", rm, "--no-page", "--commit"],
                 ["load", "--recheck", P_NEW, "--commit"]):
        rc, text, _ = e.cmd(argv)
        if rc != 0 or "REJECTED" in text or "revised" in text \
                or " new," in text or "unchanged" not in text:
            out.append(f"{' '.join(str(a) for a in argv[:5])}: rc {rc}: "
                       f"{text[-140:]}")
    for t in TAGS:
        after = (tl.count(e.cur, ED[t]), tl.count(e.cur, f"zz_s6_{t}"))
        if after != before[t]:
            out.append(f"{t}: editions or live rows changed {before[t]} -> "
                       f"{after}")
        if {o for _, _, o, _ in tl.ledger(e.cur, t)} != {"unchanged"}:
            out.append(f"{t}: a ledger row is not 'unchanged'")
    rc, text, _ = e.cmd(["refresh-latest"])
    if text.count("would write: none") != 4:
        out.append("refresh-latest plans a write after the re-reads")
    return out


def gate_13_byte_identical_reread(cur):
    name = ("a byte-identical re-read is unchanged for every period on every "
            "path, against edition 1 'as loaded' (Reg_02 NULLs and zeros "
            "included)")
    problems = []
    out = {}

    def body(e, paths, legacy, files):
        rc, text, _ = mig(e, paths, legacy, files)
        if rc != 0 or "0 differences" not in text:
            problems.append(f"migrate-legacy: rc {rc}: {text[-150:]}")
            return
        e.cur.execute("SELECT COUNT(*) FILTER (WHERE people IS NULL AND "
                      "source_marker = '*'), COUNT(*) FILTER (WHERE people = "
                      f"0) FROM public.{ED['groups']}")
        out["cells"] = e.cur.fetchone()
        d = Path(e.root) / "disk"
        d.mkdir()
        for p in paths:
            (d / p.name).write_bytes(p.read_bytes())
        with las(4):
            out["real"] = real_held_reread(e.cur, tl.ZZ, files, d, 4)
        problems.extend(reread_paths(e, paths, files))
        # a file with other content is not edition 1
        other = write_reg02(Path(e.root) / "disk2" / paths[3].name,
                            "2026-06-30", values={"E06000001": {0: 11}})
        with las(4):
            ok, detail = real_held_reread(
                e.cur, tl.ZZ, {**files, "reg02_jun": {
                    **files["reg02_jun"], "sha256": m.content_sha256(other)}},
                Path(other).parent, 4)
        if ok:
            problems.append("a file with other content was called equal")
    legacy_world(cur, body)
    if not out.get("real", (False,))[0]:
        problems.append(f"re-read check on the migrated fixture: "
                        f"{out.get('real')}")
    if out.get("cells") and not (out["cells"][0] and out["cells"][1]):
        problems.append(f"the fixture has no Reg_02 NULL or zero cells "
                        f"{out['cells']}")
    seeded = not problems
    sd = ("seeded: after migrate-legacy the same bytes read by the page, "
          "--recheck, --release, --file (with and without the page) and "
          "with --commit are unchanged for every period of every table, "
          "store no edition or live row and add only 'unchanged' ledger "
          "rows, refresh-latest plans nothing, and the re-read content equals "
          f"edition 1 as loaded ({out.get('cells', ('?', '?'))[0]} Reg_02 "
          f"'*' NULLs and {out.get('cells', ('?', '?'))[1]} zeros "
          "included); a file with other content is not equal" if seeded
          else "; ".join(problems[:3]))
    mixed(13, name, seeded, sd, cur, real_held_reread)


# ---------------------------------------------------------------- gate 14

def gate_14_revision_and_refresh(cur):
    name = ("a revision, then refresh-latest: only that period changes, "
            "source_edition on every row, loaded_at copied; key changes "
            "only with --accept-key-changes")
    problems = []

    def rows_hash(c, table, period):
        c.execute(f"SELECT md5(string_agg(t::text, ',' ORDER BY t::text)) "
                  f"FROM public.{table} t WHERE period_ending = %s",
                  (period,))
        return c.fetchone()[0]

    def body(e):
        e.cur.execute("UPDATE public.zz_s6_support SET loaded_at = "
                      "'2026-01-01'")
        first = tl.live_people(e.cur, "E06000001", P_REV)
        other = {t: rows_hash(e.cur, f"zz_s6_{t}", P_MAR) for t in TAGS}
        prior = rows_hash(e.cur, "zz_s6_support", P_FIRST)
        e.d11(Q4, values={(P_REV, "E06000001", "Section 95"): first + 3})
        rc, text, _ = e.cmd(["load", "--only", "d11", "--commit"])
        if rc != 0 or tl.editions(e.cur, "support", P_REV) != [
                (1, None, 5), (2, 1, 5)] or tl.editions(
                e.cur, "support", P_MAR) != [(1, None, 5)] or tl.editions(
                e.cur, "non_england", P_REV) != [(1, None, 2)]:
            problems.append(f"the revision did not store edition 2 of "
                            f"{P_REV} in the support table only: rc {rc}")
        if tl.live_people(e.cur, "E06000001", P_REV) != first:
            problems.append("load changed the live table before "
                            "refresh-latest")
        if pe.status(e.cur, m.profile("support"))["pending_refresh"] != [
                P_REV]:
            problems.append("status does not name the pending refresh")
        snap = state(e.cur)
        rc, text, logged = e.cmd(["refresh-latest"])
        if rc != 0 or state(e.cur) != snap or logged.called:
            problems.append("the refresh-latest preview wrote")
        rc, text, logged = e.cmd(["refresh-latest", "--commit"])
        if rc != 0:
            problems.append(f"refresh-latest --commit: rc {rc}: {text[-120:]}")
        if tl.live_people(e.cur, "E06000001", P_REV) != first + 3:
            problems.append("refresh-latest did not apply the revision")
        e.cur.execute("SELECT COUNT(*) FROM public.zz_s6_support WHERE "
                      "period_ending = %s AND people <> 0", (P_REV,))
        # only that one row's cell changed
        e.cur.execute(f"SELECT COUNT(*) FROM public.zz_s6_support l JOIN "
                      f"public.{ED['support']} e ON (e.period_ending, "
                      "e.lad24cd, e.support_type, e.accommodation_type) = "
                      "(l.period_ending, l.lad24cd, l.support_type, "
                      "l.accommodation_type) WHERE e.edition = 1 AND "
                      "l.period_ending = %s AND l.people <> e.people",
                      (P_REV,))
        if e.cur.fetchone()[0] != 1:
            problems.append("refresh-latest changed other cells of the "
                            "period")
        if tl.labels(e.cur, "support", P_REV) != [JUNE]:
            problems.append("source_edition is not the tip's on every row of "
                            f"the period: {tl.labels(e.cur, 'support', P_REV)}")
        if tl.labels(e.cur, "support", P_FIRST) != [MARCH] \
                or tl.labels(e.cur, "non_england", P_REV) != [MARCH]:
            problems.append("another period's or table's source_edition "
                            "moved")
        e.cur.execute(f"SELECT DISTINCT loaded_at FROM public.{ED['support']}"
                      " WHERE edition = 2")
        ed_loaded = e.cur.fetchall()
        e.cur.execute("SELECT COUNT(*), MIN(lad24cd) FROM public.zz_s6_"
                      "support WHERE period_ending = %s AND loaded_at = %s",
                      (P_REV, ed_loaded[0][0] if ed_loaded else None))
        n, lad = e.cur.fetchone()
        if len(ed_loaded) != 1 or n < 1 or tl.count(
                e.cur, "zz_s6_support", "WHERE period_ending = %s AND "
                "loaded_at <> '2026-01-01' AND loaded_at <> %s",
                (P_REV, ed_loaded[0][0])):
            problems.append("loaded_at was not copied from the edition to "
                            "the refreshed rows (and only those)")
        if rows_hash(e.cur, "zz_s6_support", P_FIRST) != prior or any(
                rows_hash(e.cur, f"zz_s6_{t}", P_MAR) != h
                for t, h in other.items()):
            problems.append("refresh-latest changed another period")
        if not all(pe.status(e.cur, m.profile(t))["ok"] for t in TAGS):
            problems.append("status not clean after the refresh")
        rc, text, _ = e.cmd(["refresh-latest"])
        if text.count("would write: none") != 4:
            problems.append("a second refresh-latest plans a write")
    scenario(cur, body)

    def keys(e):
        e.cur.execute("SELECT COUNT(*) FROM public.zz_s6_support")
        n0 = e.cur.fetchone()[0]
        e.d11(Q4, drop=[(P_REV, "E07000223", "Section 95")],
              add=[[dtext(P_REV), "Section 95", "North East",
                    "Redcar and Cleveland", "E06000003",
                    "Dispersal Accommodation", 6]], published=date(2026, 9, 1))
        rc, text, _ = e.cmd(["load", "--only", "d11", "--commit",
                             "--acknowledge", P_REV])
        rc, text, _ = e.cmd(["refresh-latest"])
        if "keys added 1 (E06000003/Section 95/Dispersal Accommodation); " \
                "removed 1 (E07000223/Section 95/Subsistence Only)" \
                not in text:
            problems.append(f"the plan does not name the key changes: "
                            f"{text[-200:]}")
        rc, text, _ = e.cmd(["refresh-latest", "--commit"])
        if not (rc == "halt" and "not named" in text):
            problems.append(f"key changes applied without "
                            f"--accept-key-changes: rc {rc}")
        rc, text, _ = e.cmd(["refresh-latest", "--commit",
                             "--accept-key-changes", P_FIRST])
        if rc != "halt":
            problems.append("a wrong period named in --accept-key-changes "
                            "was accepted")
        rc, text, _ = e.cmd(["refresh-latest", "--commit",
                             "--accept-key-changes", P_REV])
        if rc != 0 or "1 inserted, 1 deleted" not in text or \
                tl.live_people(e.cur, "E06000003", P_REV) != 6 or \
                tl.live_people(e.cur, "E07000223", P_REV, "Section 95",
                               "Subsistence Only") is not None or \
                tl.labels(e.cur, "support", P_REV) != [JUNE]:
            problems.append(f"--accept-key-changes: rc {rc}: {text[-160:]}")
        if not pe.status(e.cur, m.profile("support"))["ok"]:
            problems.append("status not clean after the key changes")
    scenario(cur, keys)
    report(14, name, not problems, "a revised quarter waits for "
           "refresh-latest (preview writes nothing); the commit changes "
           "exactly the revised cell, leaves the other periods and tables "
           "alone, sets source_edition to the tip's on every row of the "
           "period and copies the edition's loaded_at; added or removed "
           "keys halt without --accept-key-changes, a wrong period is "
           "refused, and the named period inserts and deletes exactly the "
           "planned keys" if not problems else "; ".join(problems[:3]))


# ---------------------------------------------------------------- gate 15

def hash_fields(h):
    """The note's scan-safe form of a sha256: two 32-hex halves in two
    labelled fields (the credential scan flags a 40+ hex run)."""
    return f"sha256-first32={h[:32]} sha256-last32={h[32:]}"


def note_hashes(note):
    """{(live table, period): (rows, sha256)} from lines `before-state
    <live table> <period> rows=<n> sha256-first32=<32 hex> sha256-last32=<32
    hex>` of the decision note (the two halves joined), or {}. Other lines
    do not match."""
    p = Path(note)
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8")
    return {(mt.group(1), mt.group(2)): (int(mt.group(3)),
                                         mt.group(4) + mt.group(5))
            for mt in re.finditer(
                r"^before-state ([a-z_0-9]+) ([0-9]{4}-[0-9]{2}-[0-9]{2}) "
                r"rows=([0-9]+) sha256-first32=([0-9a-f]{32}) "
                r"sha256-last32=([0-9a-f]{32})\s*$", text, re.M)}


def real_edition1_hash(cur, specs=REAL, note=NOTE, legacy=None):
    """Edition 1 'as loaded' of every migrated table-period equals the
    before-state line recorded in the decision note: the row count and the
    rows_content_sha256 (key + compared columns); the rows of a table sum to
    the surveyed live count."""
    for spec in specs.values():
        _need(cur, spec)
    legacy = m.LEGACY_LIVE if legacy is None else legacy
    recorded = note_hashes(note)
    bad, parts, seen = [], [], set()
    if not Path(note).exists():
        bad.append(f"decision note {Path(note).name} not written")
    for tag, spec in specs.items():
        cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                    f"public.{spec.editions_table} WHERE edition = 1 AND "
                    "source_file LIKE 'as loaded: %%' ORDER BY 1")
        migrated = [str(r[0]) for r in cur.fetchall()]
        if not migrated:
            bad.append(f"{spec.live_table}: no edition 1 'as loaded' "
                       "(migrate-legacy not run)")
        total = 0
        for p in migrated:
            recs = m.records(cur, spec, p, 1)
            n, h = len(recs), m.rows_content_sha(spec, recs)
            total += n
            key = (spec.live_table, p)
            seen.add(key)
            if Path(note).exists():
                if key not in recorded:
                    bad.append(f"the decision note records no 'before-state "
                               f"{spec.live_table} {p} rows=.. sha256-first32"
                               "=.. sha256-last32=..' line")
                elif recorded[key] != (n, h):
                    bad.append(f"{spec.live_table} {p}: edition 1 {n} rows "
                               f"sha256 {h[:16]}.. differs from the note's "
                               f"{recorded[key][0]} rows "
                               f"{recorded[key][1][:16]}..")
        want = legacy[spec.live_table][0]
        if migrated and total != want:
            bad.append(f"{spec.live_table}: edition 1 holds {total:,} rows "
                       f"over {len(migrated)} period(s), surveyed {want:,}")
        parts.append(f"{spec.live_table} {len(migrated)} period(s), "
                     f"{total:,} rows")
    for key in recorded:
        if key not in seen:
            bad.append(f"the note has a line for {key[0]} {key[1]}, which "
                       "has no edition 1 as loaded")
    return not bad, "; ".join(bad[:3]) if bad else (
        "edition 1 of every migrated table-period hashes to the before-state "
        "line in the decision note (" + "; ".join(parts) + ")")


def gate_15_before_state_hash(cur):
    name = ("edition 1 equals the before-state hash recorded in the "
            "decision note")
    out = {}

    def body(e, paths, legacy, files):
        lg = {f"zz_s6_{t}": (n, h, ld) for t in TAGS for n, h, ld in [(
            *m.live_state(e.cur, tl.ZZ[t])[:2],
            legacy[f"zz_s6_{t}"][2])]}
        rc, text, _ = mig(e, paths, lg, files)
        if rc != 0:
            out["error"] = text[-200:]
            return
        rows = {}
        lines = {}
        for t in TAGS:
            spec = tl.ZZ[t]
            e.cur.execute(f"SELECT DISTINCT period_ending FROM public."
                          f"{spec.editions_table} ORDER BY 1")
            for (p,) in e.cur.fetchall():
                p = str(p)
                recs = m.records(e.cur, spec, p, 1)
                rows[(spec.live_table, p)] = (len(recs),
                                              m.rows_content_sha(spec, recs))
                live = m.records(e.cur, spec, p)
                if rows_equal := (m.rows_content_sha(spec, live)
                                  == rows[(spec.live_table, p)][1]):
                    pass
                out.setdefault("same", True)
                out["same"] = out["same"] and rows_equal
        for (tab, p), (n, h) in rows.items():
            lines[(tab, p)] = (f"before-state {tab} {p} rows={n} "
                               f"{hash_fields(h)}")
        survey = {tab: (sum(n for (t2, _), (n, _) in rows.items()
                            if t2 == tab), None, None)
                  for tab in {t for t, _ in rows}}
        with tempfile.TemporaryDirectory() as tmp:
            note = Path(tmp) / "note.md"
            text = "intro\n" + "\n".join(lines.values()) + "\n"
            note.write_text(text, encoding="utf-8")
            out["ok"] = real_edition1_hash(e.cur, tl.ZZ, note, survey)
            k = next(iter(lines))
            h = rows[k][1]
            note.write_text(text.replace(h[:32], "0" * 32), encoding="utf-8")
            out["bad_hash"] = real_edition1_hash(e.cur, tl.ZZ, note, survey)
            note.write_text(text.replace(lines[k], ""), encoding="utf-8")
            out["no_line"] = real_edition1_hash(e.cur, tl.ZZ, note, survey)
            note.write_text(text.replace(f"{k[1]} rows={rows[k][0]} ",
                                         f"{k[1]} rows={rows[k][0] + 1} "),
                            encoding="utf-8")
            out["bad_rows"] = real_edition1_hash(e.cur, tl.ZZ, note, survey)
            note.write_text(text + lines[k].replace(k[1], "2030-03-31")
                            + "\n", encoding="utf-8")
            out["extra"] = real_edition1_hash(e.cur, tl.ZZ, note, survey)
            note.write_text(text.replace("before-state ", "before-state-all ",
                                         1), encoding="utf-8")
            out["wrong_prefix"] = real_edition1_hash(e.cur, tl.ZZ, note,
                                                     survey)
            note.write_text(text, encoding="utf-8")
            out["no_note"] = real_edition1_hash(e.cur, tl.ZZ,
                                                Path(tmp) / "x", survey)
            wrong = {t: (n + 5, h, l) for t, (n, h, l) in survey.items()}
            out["wrong_survey"] = real_edition1_hash(e.cur, tl.ZZ, note, wrong)
            # the note's 64-hex form is not what the scan accepts: the line
            # form carries two 32-hex halves only
            out["scan_safe"] = not re.search(r"[0-9a-f]{40,}", text)
        e.cur.execute("UPDATE public.zz_s6_support SET people = people + 1 "
                      "WHERE lad24cd = 'E06000001' AND period_ending = %s "
                      "AND support_type = 'Section 95'", (P_NEW,))
        out["changed"] = m.live_state(e.cur, tl.ZZ["support"])[:2] != (
            lg["zz_s6_support"][0], lg["zz_s6_support"][1])
        rc, text, _ = mig(e, paths, lg, files)
        out["twice"] = rc == "halt" and ("runs once" in text
                                         or "not as surveyed" in text)
    try:
        legacy_world(cur, body)
    except (psycopg2.Error, SystemExit, RuntimeError, AssertionError) as ex:
        return report(15, name, False, _first(ex))
    ok = (out.get("same") and out.get("ok", (0,))[0]
          and not out["bad_hash"][0] and "differs" in out["bad_hash"][1]
          and not out["no_line"][0] and "records no" in out["no_line"][1]
          and not out["bad_rows"][0] and not out["extra"][0]
          and "has no edition 1" in out["extra"][1]
          and not out["wrong_prefix"][0] and not out["no_note"][0]
          and "not written" in out["no_note"][1]
          and not out["wrong_survey"][0] and out.get("scan_safe")
          and out.get("changed") and out.get("twice"))
    mixed(15, name, ok,
          "a seeded migration stores edition 1 whose per-table, per-period "
          "hashes equal the live rows' and the note's lines (two 32-hex "
          "halves in labelled fields: scan-safe); a wrong hash, a wrong row "
          "count, a missing line, an extra line, a line with another "
          "prefix, a missing note and a wrong surveyed count fail; a later "
          "change shows; a second migration is refused" if ok
          else f"seeded: {out}", cur, real_edition1_hash)


# ---------------------------------------------------------------- gate 16

def real_migration_proof(cur, specs=REAL, files=None, raw_dir=None,
                         n_las=REAL_LAS):
    """The migration proof re-run read-only from the files on disk (found by
    sha256): the parser on the held files reproduces every cell of edition 1
    'as loaded' (key, period and every compared column: 0 differences), the
    files reconcile, and edition 1 is the 'as loaded' one."""
    for spec in specs.values():
        _need(cur, spec)
    files = m.LEGACY_FILES if files is None else files
    ok, detail = real_reconciliation(cur, files, raw_dir, n_las)
    if not ok:
        return False, detail
    paths, bad = read_held(cur, files, raw_dir, which=("d11", "reg02_mar",
                                                       "reg02_jun"))
    if bad:
        return False, "; ".join(bad[:2])
    with las(n_las):
        f, by, regs = build_held(cur, paths)
    diffs, cells, rows = [], 0, 0
    for tag in D11:
        spec = specs[tag]
        held = [r for p in sorted(by) if by[p][tag]
                for r in m.records(cur, spec, p, 1)]
        built = [r for p in sorted(by) for r in by[p][tag]]
        c, d = m._proof(tag, held, built)
        cells, rows = cells + c, rows + len(held)
        diffs += d
    spec = specs["groups"]
    held, built = [], []
    for k, g, p, recs in regs:
        held += m.records(cur, spec, p, 1)
        built += recs
    c, d = m._proof("groups", held, built)
    cells, rows, diffs = cells + c, rows + len(held), diffs + d
    for tag, spec in specs.items():
        cur.execute(f"SELECT COUNT(*) FROM public.{spec.editions_table} WHERE "
                    "edition = 1 AND source_file NOT LIKE 'as loaded: %%'")
        if cur.fetchone()[0]:
            return False, (f"{spec.editions_table}: edition 1 rows that are "
                           "not the 'as loaded' ones")
    return not diffs, (f"{len(diffs)} differences, e.g. {diffs[:2]}"
                       if diffs else
                       f"{cells:,} cells of edition 1 ({rows:,} rows over "
                       "the four tables) equal the held files read now (0 "
                       "differences); the files reconcile")


def gate_16_migration_proof(cur):
    name = ("the migration proof re-run read-only from the files on disk; "
            "seeded: proof passes, a planted held difference stops "
            "migrate-legacy and stores nothing")
    problems = []

    def ok_world(e, paths, legacy, files):
        rc, text, _ = mig(e, paths, legacy, files)
        if rc != 0 or "0 differences" not in text:
            problems.append(f"proof did not pass: {text[-150:]}")
            return
        raw = Path(e.root) / "raw"
        raw.mkdir()
        for p in paths:
            (raw / p.name).write_bytes(p.read_bytes())
        with las(4):
            ok, detail = real_migration_proof(e.cur, tl.ZZ, files, raw, 4)
            if not ok or "0 differences" not in detail:
                problems.append(f"re-run from disk: {detail}")
            bad = {**files, "d11": {**files["d11"], "sha256": "0" * 64}}
            ok, detail = real_migration_proof(e.cur, tl.ZZ, bad, raw, 4)
            if ok or "no file" not in detail:
                problems.append("a file with another sha256 was accepted")
            # a file changed on disk is not found by sha256
            (raw / paths[2].name).write_bytes(paths[2].read_bytes() + b"\0")
            ok, detail = real_migration_proof(e.cur, tl.ZZ, files, raw, 4)
            if ok or "no file" not in detail:
                problems.append("a changed file on disk was accepted")
            # a different valid Reg_02 stands in for the held one
            other = write_reg02(Path(e.root) / "raw2" / paths[3].name,
                                "2026-06-30", values={"E06000001": {0: 11}})
            ok, detail = real_migration_proof(
                e.cur, tl.ZZ, {**files, "reg02_jun": {
                    **files["reg02_jun"], "sha256": m.content_sha256(other)}},
                other.parent, 4)
            if ok or ("differences" not in detail and "no file" not in detail):
                problems.append("a file that differs from edition 1 passed")

    def planted(sql, needle, surveyed_first=False):
        def f(e, paths, legacy, files):
            lg = None
            if surveyed_first:
                lg = {k: v for k, v in legacy.items()}
                for t in TAGS:
                    n, h, ld = legacy[f"zz_s6_{t}"]
                    lg[f"zz_s6_{t}"] = (*m.live_state(e.cur, tl.ZZ[t])[:2],
                                        ld)
            e.cur.execute(sql)
            if lg is None:
                lg = {}
                for t in TAGS:
                    n, h, ld = legacy[f"zz_s6_{t}"]
                    lg[f"zz_s6_{t}"] = (*m.live_state(e.cur, tl.ZZ[t])[:2],
                                        ld)
            before = state(e.cur)
            rc, text, logged = mig(e, paths, lg, files)
            if not (rc == "halt" and needle in text
                    and state(e.cur) == before and not logged.called):
                problems.append(f"planted {needle!r}: rc {rc}: "
                                f"{text[-150:]}")
        return f
    legacy_world(cur, ok_world)
    legacy_world(cur, planted(
        "UPDATE public.zz_s6_support SET people = people + 1 WHERE lad24cd = "
        "'E06000001' AND period_ending = '2026-06-30'", "proof failed"))
    legacy_world(cur, planted(
        "UPDATE public.zz_s6_support SET published_la_name = 'Old Name' "
        "WHERE lad24cd = 'E06000002' AND period_ending = '2025-12-31' AND "
        "support_type = 'Section 4'", "proof failed"))
    legacy_world(cur, planted(
        "UPDATE public.zz_s6_groups SET percentage_of_population = "
        "percentage_of_population + 0.0001 WHERE lad24cd = 'E06000001' AND "
        "pathway = 'all_pathways' AND period_ending = '2026-06-30'",
        "proof failed"))
    legacy_world(cur, planted(
        "UPDATE public.zz_s6_non_england SET country = 'Wales' WHERE "
        "lad_code = 'S12000033' AND period_ending = '2026-03-31'",
        "proof failed"))
    legacy_world(cur, planted(
        "DELETE FROM public.zz_s6_support WHERE lad24cd = 'E06000001' AND "
        "period_ending = '2026-06-30' AND support_type = 'Section 95'",
        "not as surveyed", surveyed_first=True))
    mixed(16, name, not problems,
          "seeded migration proof passes (0 differences) and re-runs from "
          "the files by sha256; a changed or missing file and a file that "
          "is not edition 1 are caught; planted differences in a count, a "
          "name, a ratio and a country, and a dropped row, stop "
          "migrate-legacy with nothing stored" if not problems
          else "; ".join(problems[:3]), cur, real_migration_proof)


# ---------------------------------------------------------- gates 17 - 19

def gate_17_rerun(cur):
    name = "rerun idempotent: a second load changes nothing"
    problems = []

    def body(e):
        p11, p09 = e.d11(Q3)       # the files already loaded by the seed
        rp = e.reg("2026-03-31")
        snap = tables_state(e.cur)
        led = {t: len(tl.ledger(e.cur, t)) for t in TAGS}
        for argv in (["load", "--commit"],
                     ["load", "--only", "d11", "--release", "March 2026",
                      "--commit"],
                     ["load", "--file", p11, "--d09-file", p09, "--commit"],
                     ["load", "--file", p11, "--no-page", "--no-d09",
                      "--commit"],
                     ["load", "--file", rp, "--commit"],
                     ["load", "--file", rp, "--no-page", "--commit"],
                     ["refresh-latest", "--commit"]):
            rc, text, logged = e.cmd(argv)
            if rc != 0 or tables_state(e.cur) != snap:
                problems.append(f"{' '.join(map(str, argv[:3]))}: changed "
                                f"the editions or live tables: rc {rc}")
        for t in TAGS:
            if any(o != "unchanged" for _, _, o, _ in
                   tl.ledger(e.cur, t)[led[t]:]):
                problems.append(f"{t}: a re-read ledger row is not "
                                "'unchanged'")
        before = state(e.cur)
        rc, text, logged = e.cmd(["load", "--commit"])
        if rc != 0 or state(e.cur) != before or logged.called:
            problems.append("a ledger-skipped load changed state or logged")
        rc, text, logged = e.cmd(["load", "--recheck", P_MAR, "--commit"])
        if rc != 0 or tl.editions(e.cur, "support", P_MAR) != [(1, None, 5)]:
            problems.append("a recheck stored an edition")
    scenario(cur, body)
    report(17, name, not problems, "a second load by the page, --release, "
           "--file and --file --no-page and a refresh-latest stored no "
           "edition and changed no live row (re-reads add 'unchanged' "
           "ledger rows only); a ledger-skipped load logs nothing"
           if not problems else "; ".join(problems[:3]))


def gate_18_stranded(cur):
    name = "stranded period repair (edition held, live rows missing)"
    problems = []

    def body(e):
        e.cur.execute("DELETE FROM public.zz_s6_support WHERE period_ending "
                      "= %s", (P_MAR,))
        e.cur.execute("DELETE FROM public.zz_s6_groups")
        if pe.status(e.cur, m.profile("support"))["live_missing"] != [P_MAR]:
            problems.append("status does not name the stranded D11 period")
        if pe.status(e.cur, m.profile("groups"))["live_missing"] != [P_MAR]:
            problems.append("status does not name the stranded Reg_02 period")
        rc, text, _ = e.cmd(["load", "--commit"])
        if rc != 0 or "live-missing" not in text or tl.editions(
                e.cur, "support", P_MAR) != [(1, None, 5)] or tl.count(
                e.cur, "zz_s6_support", "WHERE period_ending = %s",
                (P_MAR,)) != 5 or tl.count(e.cur, "zz_s6_groups") != 48:
            problems.append(f"repair failed: rc {rc}: {text[-140:]}")
        for t in ("support", "groups"):
            if "live-missing" not in [o for _, _, o, _ in tl.ledger(e.cur, t)]:
                problems.append(f"{t}: no live-missing ledger row")
            if tl.labels(e.cur, t, P_MAR) != [MARCH]:
                problems.append(f"{t}: repaired rows do not carry the tip's "
                                "source_edition")
        if not all(pe.status(e.cur, m.profile(t))["ok"] for t in TAGS) or any(
                load_checks.check_latest_equals_live(e.cur, tl.ZZ[t])
                for t in TAGS):
            problems.append("the repaired rows do not equal the tips")
    scenario(cur, body)
    report(18, name, not problems, f"a stranded {P_MAR} (support and "
           "Reg_02) was rebuilt from its edition 1 with no new edition, the "
           "tip's source_edition and a live-missing ledger row"
           if not problems else "; ".join(problems[:3]))


def gate_19_restore(cur):
    name = "restore-edition round trip"
    problems = []

    def body(e):
        first = tl.live_people(e.cur, "E06000001", P_REV)
        e.d11(Q4, values={(P_REV, "E06000001", "Section 95"): first + 3})
        e.ok(e.cur, ["load", "--only", "d11", "--commit"])
        e.ok(e.cur, ["refresh-latest", "--commit"])
        snap = state(e.cur)
        for argv in (["restore-edition", "zz_s6_support", P_REV, "1"],
                     ["restore-edition", "zz_s6_support", P_REV, "1",
                      "--simulate"]):
            rc, text, logged = e.cmd(argv)
            if rc != 0 or state(e.cur) != snap or logged.called:
                problems.append("restore-edition preview wrote")
        rc, text, logged = e.cmd(["restore-edition", "zz_s6_support", P_REV,
                                  "1", "--commit"])
        if rc != 0 or tl.editions(e.cur, "support", P_REV)[-1] != (3, 2, 5) \
                or not logged.called:
            problems.append(f"restore --commit: rc {rc}: {text[-120:]}")
        if tl.live_people(e.cur, "E06000001", P_REV) != first + 3:
            problems.append("restore-edition touched live before "
                            "refresh-latest")
        e.ok(e.cur, ["refresh-latest", "--commit"])
        if tl.live_people(e.cur, "E06000001", P_REV) != first or \
                core.rows_differing(e.cur, tl.ZZ["support"], P_REV, 3):
            problems.append("refresh-latest did not apply the restored "
                            "edition")
        if tl.labels(e.cur, "support", P_REV) != [MARCH]:
            problems.append("live source_edition does not name the restored "
                            "release")
        ed1 = {tuple(sorted(r.items())) for r in m.records(
            e.cur, tl.ZZ["support"], P_REV, 1)}
        ed3 = {tuple(sorted(r.items())) for r in m.records(
            e.cur, tl.ZZ["support"], P_REV, 3)}
        if ed1 != ed3:
            problems.append("edition 3 does not hold edition 1's rows")
        for argv in (["restore-edition", "zz_s6_support", P_REV, "3",
                      "--commit"],
                     ["restore-edition", "zz_s6_support", P_REV, "9",
                      "--commit"],
                     ["restore-edition", "no_such_table", P_REV, "1",
                      "--commit"]):
            ok, _ = e.halts(argv, state(e.cur))
            if not ok:
                problems.append(f"{' '.join(argv[:4])} did not halt")
        if not all(pe.status(e.cur, m.profile(t))["ok"] for t in TAGS):
            problems.append("status not clean")
        # Reg_02
        rg = tl.count(e.cur, ED["groups"])
        e.d11(Q4)           # no change to Asy_D11
        e.reg("2026-03-31", values={"E06000001": {0: 11}},
              published=date(2026, 9, 1))
        e.ok(e.cur, ["load", "--only", "reg02", "--commit"])
        e.ok(e.cur, ["refresh-latest", "--commit"])
        e.ok(e.cur, ["restore-edition", "zz_s6_groups", P_MAR, "1",
                     "--commit"])
        e.ok(e.cur, ["refresh-latest", "--commit"])
        if core.rows_differing(e.cur, tl.ZZ["groups"], P_MAR, 3) or \
                tl.labels(e.cur, "groups", P_MAR) != [MARCH]:
            problems.append("the Reg_02 restore did not round-trip")
    scenario(cur, body)
    report(19, name, not problems, "edition 1 stored as edition 3 (its "
           "release label kept, 'restored from edition 1' added) and "
           "applied by refresh-latest in Asy_D11 and Reg_02; the tip, a "
           "missing edition and an unknown table are refused; a preview "
           "writes nothing" if not problems else "; ".join(problems[:3]))


# ---------------------------------------------------------------- gate 20

def _write_sql_problems(path):
    """Tables a module's SQL string constants write to (insert into,
    update, delete from, truncate) by a literal name: the ones that are not
    the loader's own."""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    own = {s.live_table for s in REAL.values()} | {
        s.editions_table for s in REAL.values()} | {"pipeline_run_log"}
    pat = re.compile(r"(?i)^\s*(?:insert\s+into|update|delete\s+from|"
                     r"truncate(?:\s+table)?)\s+(?:public\.)?"
                     r"([a-z_][a-z0-9_]*)(?![a-z0-9_.{])")
    bad = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for t in pat.findall(node.value):
                if t not in own:
                    bad.add(t)
    return sorted(bad)


def real_series_and_view(cur, breaks="public.asylum_series_breaks"):
    cur.execute("SELECT first_period, last_period, support_type, dimension, "
                f"description, comparability FROM {breaks} "
                "ORDER BY first_period, support_type")
    got = [tuple(r) for r in cur.fetchall()]
    want = sorted(m.SERIES_BREAKS, key=lambda r: (r[0], r[2]))
    bad = []
    if got != want:
        bad.append(f"asylum_series_breaks holds {len(got)} row(s) that differ"
                   f" from SERIES_BREAKS ({len(want)}): "
                   f"{[r[:2] for r in got][:3]}")
    cur.execute("SELECT column_name, data_type FROM information_schema."
                "columns WHERE table_schema = 'public' AND table_name = %s "
                "ORDER BY ordinal_position", (VIEW,))
    cols = [tuple(r) for r in cur.fetchall()]
    if cols != list(VIEW_COLUMNS):
        bad.append(f"{VIEW} columns changed: {cols}")
    cur.execute("SELECT pg_get_viewdef(%s::regclass)", (f"public.{VIEW}",))
    text = cur.fetchone()[0]
    if "_editions" in text:
        bad.append(f"{VIEW} reads an editions table")
    wrote = _write_sql_problems(LOADER)
    if wrote:
        bad.append(f"the loader writes to {wrote}")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"asylum_series_breaks equals SERIES_BREAKS ({len(want)} rows, "
        f"break_id ignored); {VIEW} still has its {len(VIEW_COLUMNS)} "
        "columns and reads the live table; the loader writes only the four "
        "live tables, their editions tables and the run log")


def gate_20_series_breaks_and_view(cur):
    name = ("asylum_series_breaks equals SERIES_BREAKS (ignoring break_id) "
            "and the totals view's columns are unchanged; the loader writes "
            "only its own tables")
    problems = []

    def body(c):
        ok, detail = real_series_and_view(c)
        if not ok:
            problems.append(f"the real state: {detail}")
        # planted sources of the loader
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x.py"
            p.write_text('A = "INSERT INTO public.asylum_series_breaks VALUES'
                         ' (1)"\nB = "UPDATE la_boundaries SET x = 1"\n'
                         'C = "DELETE FROM public.zz_s6_support_editions"\n'
                         'D = "UPDATE public.la_asylum_support SET a = 1"\n',
                         encoding="utf-8")
            bad = _write_sql_problems(p)
            if bad != ["asylum_series_breaks", "la_boundaries",
                       "zz_s6_support_editions"]:
                problems.append(f"the write check missed a table: {bad}")
    _in_savepoint(cur, body)
    # a changed row is caught by the same comparison, on a temporary copy
    # (the real table is only read)
    def planted(c):
        c.execute("CREATE TEMP TABLE s6_breaks_copy AS SELECT * FROM "
                  "public.asylum_series_breaks")
        c.execute("UPDATE s6_breaks_copy SET description = description || "
                  "' x' WHERE break_id = (SELECT MIN(break_id) FROM "
                  "s6_breaks_copy)")
        ok, detail = real_series_and_view(c, "pg_temp.s6_breaks_copy")
        if ok or "differ" not in detail:
            problems.append("a changed series-break row was not caught")
    _in_savepoint(cur, planted)
    report(20, name, not problems, "the real asylum_series_breaks rows equal "
           "SERIES_BREAKS and the totals view keeps its columns, and the "
           "loader writes only its own tables; a changed row and a write to "
           "another table (series breaks, la_boundaries, an editions "
           "table of another name) are caught" if not problems
           else "; ".join(problems[:3]))


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
    names = [s.editions_table for s in REAL.values()] + [
        ledger_name(s) for s in REAL.values()]

    def real_tables():
        cur.execute("SELECT " + ", ".join(f"to_regclass('public.{n}')"
                                          for n in names))
        return cur.fetchone()
    real_before = real_tables()

    def body(c):
        with env(c) as e, tl.specs():
            with mock.patch.object(socket.socket, "connect", refuse), \
                    mock.patch.object(socket, "create_connection", refuse), \
                    mock.patch.object(socket, "getaddrinfo", refuse):
                e.d11(Q3)
                e.reg("2026-03-31")
                rc, _, _ = e.cmd(["load", "--commit"])
                rc_s, _, _ = e.cmd(["status"])
                rc_r, _, _ = e.cmd(["refresh-latest"])
                rc_l, _, _ = e.cmd(["load"])
                raw = e.root / "raw"
                body_a = write_d11(e.root / "dl" / "a.xlsx", Q3).read_bytes()
                body_b = write_d11(e.root / "dl" / "b.xlsx", Q4).read_bytes()
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
    real_after = real_tables()
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
           f"{runs}->{runs_after}; real editions tables and ledgers "
           f"unchanged={real_before == real_after}; secret-like literal in "
           f"source {literal or 'none'}; {len(secrets)} secret-named "
           f"settings checked against {len(files)} files and "
           f"{len(OUTPUT)} output lines: in source {in_src or 'none'}, in "
           f"output {in_out or 'none'}")


# ---------------------------------------------------------------- gate 22

MERGE_ADD = [
    [dtext(P_FIRST), "Section 95", "Yorkshire and The Humber", "Hambleton",
     "E07000164", "Dispersal Accommodation", 4],
    [dtext(P_FIRST), "Section 95", "Yorkshire and The Humber", "Craven",
     "E07000163", "Dispersal Accommodation", 6],
    [dtext(P_REV), "Section 95", "Yorkshire and The Humber", "Craven",
     "E07000163", "Dispersal Accommodation", 5],
    [dtext(P_REV), "Section 95", "Yorkshire and The Humber", "Hambleton",
     "E07000164", "Dispersal Accommodation", 3],
]


def real_row_order(cur, files=None, raw_dir=None, n_shuffles=3):
    """The held Asy_D11 file with its rows shuffled and reversed gives the
    records of the file as listed (merged authorities' published_la_name
    included), read-only."""
    files = m.LEGACY_FILES if files is None else files
    p = held_file("d11", files, raw_dir)
    if p is None:
        return False, (f"no file in {raw_dir or m.RAW_DIR} with sha256 "
                       f"{files['d11']['sha256'][:16]}")
    f = m.read_d11(p)
    rmap, problems = m.resolve_codes(cur, set(), m.d11_codes_by_period(f))
    if problems:
        return False, f"geography: {problems[0]}"
    want, merges, _ = m.build_d11_records(f, rmap)
    bad = []
    for seed in range(n_shuffles):
        sh = dict(f, rows=list(f["rows"]))
        random.Random(seed).shuffle(sh["rows"])
        if m.build_d11_records(sh, rmap)[0] != want:
            bad.append(f"shuffle {seed} gives other records")
        sh["rows"].reverse()
        if m.build_d11_records(sh, rmap)[0] != want:
            bad.append(f"shuffle {seed} reversed gives other records")
    return not bad, "; ".join(bad[:2]) if bad else (
        f"{p.name}: {len(f['rows']):,} rows shuffled {n_shuffles} times and "
        f"reversed give the same records ({len(merges)} merged keys)")


def gate_22_row_order(cur):
    name = ("the merged-authority name never depends on row order: "
            "shuffled and reversed rows give the same records and no "
            "revision")
    problems = []

    def body(e):
        rows = d11_rows(Q3, add=MERGE_ADD)
        e.d11_file(rows, Q3)
        e.ok(e.cur, ["load", "--only", "d11", "--commit"])
        spec = tl.ZZ["support"]
        e.cur.execute(f"SELECT published_la_name, people FROM public."
                      f"{spec.live_table} WHERE lad24cd = 'E06000065' AND "
                      "period_ending = %s", (P_FIRST,))
        got = e.cur.fetchall()
        if got != [("Hambleton", 10)]:
            problems.append(f"the merged name at {P_FIRST}: {got}, expected "
                            "the highest code's (Hambleton, 10 people)")
        snap = tables_state(e.cur)
        orders = []
        head, body_rows = rows[:1], rows[1:]
        orders.append(("reversed", head + body_rows[::-1]))
        for seed in (1, 2, 3):
            sh = list(body_rows)
            random.Random(seed).shuffle(sh)
            orders.append((f"shuffled {seed}", head + sh))
        for label, order in orders:
            e.d11_file(order, Q3, published=None)
            rc, text, logged = e.cmd(["load", "--only", "d11", "--commit"])
            if rc != 0 or " revised" in text or " new," in text \
                    or "REJECTED" in text or tables_state(e.cur) != snap:
                problems.append(f"{label}: rc {rc}, the file with the same "
                                f"content in another order was not "
                                f"unchanged: {text[-140:]}")
        if tl.editions(e.cur, "support", P_FIRST) != [(1, None, 6)]:
            problems.append("a reordered file stored an edition")
    scenario(cur, body, seed=False)
    seeded = not problems
    sd = ("seeded: two codes merging into one authority give the highest "
          "code's name whatever the row order, and the same content "
          "reordered (reversed, three shuffles) is unchanged: no revised "
          "edition, no rejection" if seeded else "; ".join(problems[:3]))
    if not seeded:
        return report(22, name, False, sd)
    try:
        ok, detail = real_row_order(cur)
    except (psycopg2.Error, SystemExit, ValueError, LookupError) as ex:
        return report(22, name, False, _first(ex))
    report(22, name, ok, f"{sd}; held file: {detail}")


# --------------------------------------------------------------------- main

GATES = (gate_1_table_shape_and_immutability, gate_2_edition1_and_latest,
         gate_3_provenance, gate_4_codes, gate_5_rule_1,
         gate_6_reconciliations, gate_7_identity, gate_8_older_file,
         gate_9_stop_conditions, gate_10_one_transaction,
         gate_11_ledger_skip, gate_12_preview,
         gate_13_byte_identical_reread, gate_14_revision_and_refresh,
         gate_15_before_state_hash, gate_16_migration_proof, gate_17_rerun,
         gate_18_stranded, gate_19_restore,
         gate_20_series_breaks_and_view, gate_21_no_network_no_secrets,
         gate_22_row_order)


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
    mine = [t for t in left if t.startswith("zz_s6")]
    if mine:
        print(f"LEFTOVER: committed zz_s6 tables in the database: {mine}")
        RESULTS.append(False)
    else:
        print("no zz_s6 table left in the database after the rollback")
    others = [t for t in left if t not in mine]
    if others:
        print(f"NOTE: other zz% relations exist in the database (not made "
              f"by this run): {others}")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
