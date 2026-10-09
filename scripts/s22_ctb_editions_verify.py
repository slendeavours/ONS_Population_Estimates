"""Gates for the S22 (MHCLG Council Taxbase and Live Table 615) editions tables.

Mirrors scripts/s4_care_leaver_editions_verify.py and s11_cqc_editions_verify.py.
Prints `GATE n name: PASS|FAIL`; exits 1 on any FAIL. It writes nothing, not
even a run-log row (the old s22_verify.py did). The real-table gates read
la_council_taxbase_empties, la_ctb_exemption_classes, la_vacant_dwellings_615,
their three editions tables and the two file-check ledgers. Until the editions
tables exist (ddl and migrate-legacy not yet run) they print `FAIL (pending
migration)` with the words "not yet migrated", so the script exits non-zero
until then, like the other verifiers; nothing is created to make them pass.

The seeded gates never need the real editions tables: they run on the
throwaway tables zz_s22_ctb_live / _editions, zz_s22_cls_live / _editions,
zz_s22_615_live / _editions and the two ledgers (copies of the loader's three
specs), created inside the transaction, and call the loader's own writer and
commands (s22_ctb_editions.main with the three SPECs pointed at the copies,
the content API and the downloads stubbed; apply_ctb_year; apply_615_year;
migrate_legacy; restore_edition; core.refresh_latest) rather than a
re-implementation. The real tables are only ever read.

Gates:
  1  three editions tables, the two ledgers, append-only triggers (UPDATE,
     DELETE, TRUNCATE refused), null_reasons on the three live tables;
     on the throwaway copy and, once they exist, the real tables
  2  every live period has edition 1; the latest edition equals live cell
     for cell, per spec                                              (real)
  3  CTB main and classes tips carry the same edition number per year (real)
  4  live source_publication uniform per year and equal to the tip's;
     every tip names its file date                                   (real)
  5  codes: CTB and classes in la_boundaries, no E08000038/39 in live
     lad24cd, 615 mapping_status consistent with lad24cd, no private
     recode dict in the module (AST)                                 (real)
  6  296 CTB rows and 3,256 class rows per year                     (real)
  7  NULL versus 0 never conflated (seeded per column)         (real data)
  8  derived columns NULL when a part is NULL, equal to the arithmetic
     otherwise (live and seeded)                               (real data)
  9  identity from the file: seeded cover-year, block-title and header
     mismatches halt
 10  reconciliation: the authorities sum to England; a seeded failure halts
 11  615 all vacants equals CTB empty_total + unoccupied_exemptions_total
     for every authority                                              (real)
 12  older-file guard on the page, --release and --file paths
 13  stop conditions: each seeded REJECTED, stores nothing, no ledger row
 14  a new period in one transaction (CTB: both specs and the ledger)
 15  ledger skip needs the pair for every period
 16  preview and --simulate write nothing
 17  revision then refresh-latest changes only that period, moves
     source_publication and loaded_at
 18  edition 1 of each period equals the before-state hash recorded in the
     decision note                                                   (real)
 19  the migration proof re-run read-only from the files on disk     (real)
 20  W1's subqueries on live return the tip's values for the latest year
     (four counts and the view's lte_rate_pct)                       (real)
 21  rerun idempotent
 22  stranded period repair
 23  restore-edition round trip
 24  no network, no secret in source or output, no write outside the
     transaction

Usage:
    python scripts/s22_ctb_editions_verify.py

Everything runs in one transaction that ends in a rollback; each seeded step
also runs in a savepoint that is rolled back. The commands get a stand-in
connection whose commit only counts. No network, no real-table writes, no
backend is ever terminated.
"""
import ast
import contextlib
import dataclasses
import io
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
from _db import ENV, get_conn  # noqa: E402
import editions_core as core  # noqa: E402
import geography  # noqa: E402
import load_checks  # noqa: E402
import period_editions as pe  # noqa: E402
import s22_ctb_editions as m  # noqa: E402
import test_s22_ctb_loader as tl  # noqa: E402
from test_s22_ctb_pure import (CODES, PUB_FIRST, PUB_REVISED,  # noqa: E402
                               T615_CODES, _Session, write_615, write_ctb)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# the real specs, taken before anything is patched; the real-table gates use
# only these (never m.SPEC_*, which the seeded gates point at the copies)
REAL = (m.SPEC_CTB, m.SPEC_CLASSES, m.SPEC_615)
ZZ = tl.ZZ                       # the throwaway copies, same order
Z_CTB, Z_CLS, Z_615 = ZZ
LIVE_NAMES = {s.live_table for s in REAL}
NOTE = (HERE.parent / "docs" / "decisions"
        / "2026-10-09-s22-editions-first-load.md")
NOT_YET = ("the S22 editions tables do not exist yet (not yet migrated)")
RESULTS = []
OUTPUT = []  # every line the gates print, for the secret-leak gate (24)
NEVER_CODES = ("E08000038", "E08000039")


def ledgers(specs):
    """(CTB main ledger, 615 ledger) table names of a spec triple."""
    return (f"{specs[0].editions_table}_file_checks",
            f"{specs[2].editions_table}_file_checks")


def report(n, name, ok, detail="", pending=False):
    RESULTS.append(ok)
    verdict = "PASS" if ok else ("FAIL (pending migration)" if pending
                                 else "FAIL")
    line = f"GATE {n} {name}: {verdict}" + (f"  [{detail}]" if detail else "")
    OUTPUT.append(line)
    print(line)


def _in_savepoint(cur, fn):
    cur.execute("SAVEPOINT h")
    try:
        return fn(cur)
    finally:
        cur.execute("ROLLBACK TO SAVEPOINT h")
        cur.execute("RELEASE SAVEPOINT h")


def exists(cur, specs):
    """{editions table: exists} of a spec triple."""
    return {s.editions_table: pe.table_exists(cur, s.editions_table)
            for s in specs}


def none_exist(cur):
    return not any(exists(cur, REAL).values())


def _need(cur, specs):
    miss = [t for t, ok in exists(cur, specs).items() if not ok]
    miss += [t for t in ledgers(specs) if not pe.table_exists(cur, t)]
    if miss:
        raise ValueError(f"{', '.join(miss)} does not exist")


# ------------------------------------------------------------ throwaway data

def setup_throwaway(cur):
    names = ([s.live_table for s in ZZ] + [s.editions_table for s in ZZ]
             + list(ledgers(ZZ)))
    cur.execute("SELECT " + ", ".join(f"to_regclass('public.{n}')"
                                      for n in names))
    if any(cur.fetchone()):
        sys.exit("HARD STOP: a zz_s22 table already exists as a real table; "
                 "refusing to run")
    for s, real in zip(ZZ, REAL):
        cur.execute(f"CREATE TABLE public.{s.live_table} (LIKE "
                    f"public.{real.live_table} INCLUDING ALL)")
    m.create_all(cur, ZZ)


def state(cur):
    """Everything a refused or previewed run must leave alone."""
    out = []
    for s in ZZ:
        for t in (s.live_table, s.editions_table):
            cur.execute(f"SELECT md5(string_agg(t::text, ',' ORDER BY "
                        f"t::text)) FROM public.{t} t")
            out.append(cur.fetchone()[0])
    for t in ledgers(ZZ):
        cur.execute(f"SELECT md5(string_agg(t::text, ',' ORDER BY t::text))"
                    f" FROM public.{t} t")
        out.append(cur.fetchone()[0])
    return tuple(out)


def big_codes(cur, n=40):
    """n current unitary codes from la_boundaries, plus one more."""
    cur.execute("SELECT lad24cd FROM public.la_boundaries WHERE lad24cd LIKE "
                "'E06%%' ORDER BY 1 LIMIT %s", (n + 1,))
    return [r[0] for r in cur.fetchall()]


# ------------------------------------------------------------ files and Env

class Env(tl.Fixture):
    """The loader's main(argv) on the throwaway tables, the content API and
    the downloads stubbed; every workbook is written here (write_ctb,
    write_615). big=False uses the loader tests' five authorities; big=True
    40 real unitary authorities plus Barnsley (so the 30-authority limit can
    be crossed)."""

    def __init__(self, cur, root, big=False):
        super().__init__()
        self.cur = cur
        self.root = Path(root)
        self.api, self.urls, self.final = {}, {}, {}
        self.fetched, self.docs = [], []
        self.n = 0
        self.tx = None
        if big:
            e06 = big_codes(cur)
            self.extra = e06[40]
            self.codes = e06[:40] + ["E08000038"]
            self.codes615 = e06[:40] + ["E08000016", "E08000038"]
            self.bounds = set(e06[:40]) | {"E08000016"}
        else:
            self.extra = "E06000003"
            self.codes, self.codes615 = list(CODES), list(T615_CODES)
            self.bounds = set(tl.BOUNDS)
        self.n_auth = len(self.codes)

    def ctb(self, year=2025, **kw):
        kw.setdefault("codes", self.codes)
        return super().ctb(year, **kw)

    def t615(self, years=(2023, 2024, 2025), **kw):
        kw.setdefault("codes", self.codes615)
        return super().t615(years, **kw)

    def run_main(self, cur, argv, *, bounds=None):
        """main(argv) on the throwaway tables. Returns (rc or 'halt', text,
        run-log mock); self.tx counts the commits."""
        borrowed = tl._Borrowed(cur)
        self.tx = borrowed.tx

        def fetch(url, dest, session=None):
            self.fetched.append(url)
            return self.urls[url], self.final.get(url, url)
        out = io.StringIO()
        with tl.specs(), \
                mock.patch.object(m, "CTB_AUTHORITIES", self.n_auth), \
                mock.patch.object(m, "_conn", return_value=borrowed), \
                mock.patch.object(m, "fetch_json",
                                  side_effect=lambda p, s=None: self.api[p]), \
                mock.patch.object(m, "fetch", side_effect=fetch), \
                mock.patch.object(m, "_boundaries", return_value=set(
                    self.bounds if bounds is None else bounds)), \
                mock.patch.object(m, "log_run") as logged, \
                contextlib.redirect_stdout(out):
            try:
                rc = m.main(list(argv))
            except SystemExit as e:
                rc = "halt"
                out.write(f"\n{e.code}")
            finally:
                borrowed.tx.done()
        OUTPUT.extend(out.getvalue().splitlines())
        return rc, out.getvalue(), logged

    def cmd(self, argv, **kw):
        return self.run_main(self.cur, argv, **kw)

    def commits(self):
        return self.tx.commits if self.tx else 0

    def halts(self, argv, before, needle=None, **kw):
        """(True, text) if the command halted or exited 1 and left
        everything as `before`, with no run-log row and no commit."""
        rc, text, logged = self.cmd(argv, **kw)
        ok = (rc in ("halt", 1) and state(self.cur) == before
              and not logged.called and self.commits() == 0
              and (needle is None or needle in text))
        return ok, text

    def seed_both(self, years615=(2023, 2024)):
        self.seed_ctb(self.cur, pub=PUB_FIRST)
        self.seed_615(self.cur, years615)

    # the migration world: held-style live rows (tl.Migrate.seed_legacy)
    seed_legacy = tl.Migrate.seed_legacy
    legacy = tl.Migrate.legacy

    def migrate_cmd(self, files, argv=("--commit",), legacy=None):
        """The migrate-legacy command on the seeded legacy state: the
        surveyed constants pointed at the seeded state and files."""
        ctb, t615, fx = files
        if legacy is None:
            legacy = dict(self.legacy(self.cur))
        with mock.patch.object(m, "LEGACY_LIVE", legacy),                 mock.patch.object(m, "LEGACY_FILES", fx):
            return self.cmd(["migrate-legacy", str(ctb), str(t615), *argv])


@contextmanager
def env(cur, big=False):
    with tempfile.TemporaryDirectory() as tmp:
        yield Env(cur, tmp, big)


def scenario(cur, fn, big=False):
    """Run fn(env) in a savepoint that is rolled back."""
    def body(c):
        with env(c, big) as e:
            return fn(e)
    return _in_savepoint(cur, body)


def legacy_world(cur, fn):
    """A seeded legacy state (as the old build left it) and the fixture
    files; fn(env, (ctb, t615, files))."""
    def body(c):
        with env(c) as e:
            files = e.seed_legacy(c)
            return fn(e, files)
    return _in_savepoint(cur, body)


def edit_xlsx(path, fn):
    import openpyxl
    wb = openpyxl.load_workbook(path)
    fn(wb)
    wb.save(path)


# ---------------------------------------------------------------- gate 1

_DTYPE = {"integer": "integer", "varchar": "character varying",
          "text": "text"}


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


def shape_problems(cur, specs):
    """Problems with the three editions tables, live tables and the two
    ledgers of a spec triple."""
    bad = []
    led = dict(zip((specs[0].name, specs[2].name), ledgers(specs)))
    for s in specs:
        for t in (s.editions_table, s.live_table):
            if not pe.table_exists(cur, t):
                bad.append(f"{t} does not exist")
    for t in led.values():
        if not pe.table_exists(cur, t):
            bad.append(f"{t} does not exist")
    if bad:
        return bad
    meta = {"edition": ("integer", "NO"), "supersedes": ("integer", "YES"),
            "release_label": ("text", None), "published_date": ("date", None),
            "source_file": ("text", None), "source_sha256": ("text", None),
            "loaded_at": ("timestamp with time zone", None)}
    for s in specs:
        cols, lcols = _columns(cur, s.editions_table), _columns(
            cur, s.live_table)
        need = dict(meta)
        for c in tuple(s.key_cols) + (s.period_col,):
            need[c] = ("integer" if c == s.period_col else
                       "character varying", "NO")
        for c, ty in s.value_cols + s.extra_cols:
            need[c] = (_family(ty), None)
        for c, (ty, nullable) in need.items():
            got = cols.get(c)
            if got is None:
                bad.append(f"{s.editions_table}: {c} missing")
            elif got[0] != ty or (nullable and got[1] != nullable):
                bad.append(f"{s.editions_table}: {c} is {got}, expected "
                           f"{ty} {nullable}")
        if _pk(cur, s.editions_table) != set(s.key_cols) | {
                s.period_col, "edition"}:
            bad.append(f"{s.editions_table}: primary key is not key + "
                       "period + edition")
        cur.execute("SELECT COUNT(*) FROM pg_constraint WHERE conrelid = "
                    "%s::regclass AND contype = 'f'",
                    (f"public.{s.editions_table}",))
        fks = cur.fetchone()[0]
        if fks != (1 if s.fk_la_boundaries else 0):
            bad.append(f"{s.editions_table}: {fks} foreign key(s), expected "
                       f"{1 if s.fk_la_boundaries else 0}")
        bad += _triggers(cur, s.editions_table, s.trigger,
                         s.truncate_trigger)
        for c, _ in s.value_cols + s.extra_cols:
            if c not in lcols:
                bad.append(f"{s.live_table}: {c} missing"
                           + (" (run ddl)" if c == "null_reasons" else ""))
        if lcols.get("null_reasons", ("text",))[0] != "text":
            bad.append(f"{s.live_table}: null_reasons is not text")
    for s, t in ((specs[0], led[specs[0].name]),
                 (specs[2], led[specs[2].name])):
        lc = _columns(cur, t)
        for c, ty, nullable in (("id", "bigint", "NO"),
                                (s.period_col, "integer", "NO"),
                                ("source_file", "text", "NO"),
                                ("file_sha256", "text", "NO"),
                                ("outcome", "text", "NO"),
                                ("edition", "integer", "YES"),
                                ("checked_at", "timestamp with time zone",
                                 "NO")):
            got = lc.get(c)
            if got is None or tuple(got) != (ty, nullable):
                bad.append(f"{t}: {c} is {got}, expected {(ty, nullable)}")
        cur.execute("""SELECT pg_get_constraintdef(oid) FROM pg_constraint
                       WHERE conrelid = %s::regclass AND contype = 'c'""",
                    (f"public.{t}",))
        chk = " ".join(r[0] for r in cur.fetchall())
        if not all(f"'{o}'" in chk for o in pe.FILE_CHECK_OUTCOMES):
            bad.append(f"{t}: outcome check is {chk!r}")
        bad += _triggers(cur, t, f"{s.name}_file_checks_immutable",
                         f"{s.name}_file_checks_no_truncate")
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
    name = ("three editions tables, two ledgers, live null_reasons and shape "
            "(UPDATE, DELETE and TRUNCATE are refused on the editions and "
            "the ledgers)")
    bad = shape_problems(cur, ZZ)

    def seed(cur):
        with env(cur) as e:
            e.seed_both()
    targets = [(f"editions {s.name}", s.editions_table,
                {"UPDATE": f"UPDATE public.{s.editions_table} SET edition = "
                 "edition",
                 "DELETE": f"DELETE FROM public.{s.editions_table}",
                 "TRUNCATE": f"TRUNCATE public.{s.editions_table}"})
               for s in ZZ]
    targets += [(f"ledger {t}", t,
                 {"UPDATE": f"UPDATE public.{t} SET outcome = 'new'",
                  "DELETE": f"DELETE FROM public.{t}",
                  "TRUNCATE": f"TRUNCATE public.{t}"}) for t in ledgers(ZZ)]
    for what, _, stmts in targets:
        for k, msg in immutability(cur, stmts, seed).items():
            if not (msg and "append-only" in msg):
                bad.append(f"{what} {k} not refused ({msg})")
    scope = "throwaway copy"
    if not none_exist(cur):
        scope += " and real tables"
        bad += [f"real: {p}" for p in shape_problems(cur, REAL)]
    report(1, name, not bad, "; ".join(bad[:4]) if bad else
           f"checked on the {scope}; UPDATE/DELETE/TRUNCATE raise "
           "append-only on the three editions tables and both ledgers")


# ------------------------------------------------------- real-table gates

def mixed(n, name, seeded_ok, seeded_detail, cur, real_fn):
    """A gate with a seeded part (always) and a real part (once the editions
    tables exist, else pending)."""
    if not seeded_ok:
        return report(n, name, False, f"seeded: {seeded_detail}")
    if none_exist(cur):
        return report(n, name, False, f"seeded part passes; real: {NOT_YET}",
                      pending=True)
    try:
        ok, detail = real_fn(cur)
    except (psycopg2.Error, SystemExit, ValueError, LookupError) as e:
        return report(n, name, False, str(e).splitlines()[0])
    report(n, name, ok, f"{seeded_detail}; real: {detail}")


def real_gate(cur, n, name, fn):
    """Run fn(cur) -> (ok, detail) on the real tables, or report pending
    when the editions tables have not been created."""
    if none_exist(cur):
        return report(n, name, False, NOT_YET, pending=True)
    try:
        ok, detail = fn(cur)
    except (psycopg2.Error, SystemExit, ValueError, LookupError) as e:
        return report(n, name, False, str(e).splitlines()[0])
    report(n, name, ok, detail)


EMPTY = "no live periods to check (an empty state is not a pass)"


def _live_periods(cur, spec):
    cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                f"public.{spec.live_table} ORDER BY 1")
    return [r[0] for r in cur.fetchall()]


def _tip(cur, spec, y):
    return core.chain_tip(cur, spec, str(y))


def real_edition1_and_latest(cur, specs=None):
    specs = specs or REAL
    _need(cur, specs)
    bad, parts = [], []
    for s in specs:
        periods = _live_periods(cur, s)
        if not periods:
            return False, f"{s.live_table}: {EMPTY}"
        cur.execute(f"SELECT DISTINCT {s.period_col} FROM "
                    f"public.{s.editions_table} WHERE edition = 1 AND "
                    "source_file IS NOT NULL")
        have = {r[0] for r in cur.fetchall()}
        missing = [y for y in periods if y not in have]
        if missing:
            bad.append(f"{s.live_table}: no edition 1 (with a source_file) "
                       f"for {missing[:6]} ({len(missing)} of "
                       f"{len(periods)})")
            continue
        bad += [f"{s.live_table}: {p}"
                for p in load_checks.check_latest_equals_live(cur, s)[:3]]
        parts.append(f"{s.live_table} {len(periods)}")
    return not bad, "; ".join(bad[:3]) if bad else (
        "every live period has edition 1 and its latest edition equals "
        "live cell for cell (" + ", ".join(parts) + " period(s))")


def real_pair_tips(cur, specs=None):
    specs = specs or REAL
    _need(cur, specs)
    main, cls = specs[0], specs[1]
    ym = {r for r in _live_periods(cur, main)}
    yc = {r for r in _live_periods(cur, cls)}
    if not ym:
        return False, EMPTY
    bad = []
    for y in sorted(ym | yc):
        try:
            tm, tc = _tip(cur, main, y), _tip(cur, cls, y)
        except (LookupError, ValueError) as e:
            bad.append(f"{y}: {e}")
            continue
        if tm != tc:
            bad.append(f"{y}: main tip edition {tm}, classes tip edition "
                       f"{tc}")
        cur.execute(f"SELECT COUNT(*) FROM public.{main.editions_table} "
                    f"WHERE {main.period_col} = %s AND edition = %s", (y, tm))
        a = cur.fetchone()[0]
        cur.execute(f"SELECT COUNT(*) FROM public.{cls.editions_table} "
                    f"WHERE {cls.period_col} = %s AND edition = %s", (y, tc))
        b = cur.fetchone()[0]
        if b != 11 * a:
            bad.append(f"{y}: {a} main rows but {b} class rows (expected "
                       f"{11 * a})")
    if ym != yc:
        bad.append(f"live main years {sorted(ym)} differ from live class "
                   f"years {sorted(yc)}")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{len(ym)} taxbase year(s): main and classes tips carry the same "
        "edition number and 11 class rows per authority")


_AS_LOADED = re.compile(r"^as loaded: (.*); file dated [0-9]{4}-[0-9]{2}-"
                        r"[0-9]{2}$", re.S)


def real_source_uniform(cur, specs=None):
    specs = specs or REAL
    _need(cur, specs)
    s = specs[0]
    periods = _live_periods(cur, s)
    if not periods:
        return False, EMPTY
    bad = []
    for sp in specs:
        for y in _live_periods(cur, sp):
            tip = _tip(cur, sp, y)
            cur.execute(f"SELECT DISTINCT source_file FROM "
                        f"public.{sp.editions_table} WHERE {sp.period_col} = "
                        "%s AND edition = %s", (y, tip))
            files = {r[0] for r in cur.fetchall()}
            if len(files) != 1 or m.release_rank(next(iter(files))) is None:
                bad.append(f"{sp.live_table} {y} ed{tip}: source_file "
                           f"{sorted(map(str, files))[:1]} has no single "
                           "file date")
    for y in periods:
        tip = _tip(cur, s, y)
        cur.execute(f"SELECT DISTINCT source_file FROM "
                    f"public.{s.editions_table} WHERE {s.period_col} = %s "
                    "AND edition = %s", (y, tip))
        (want,) = cur.fetchall()[0]
        mt = _AS_LOADED.match(want or "")
        if mt:
            want = mt.group(1)
        cur.execute(f"SELECT DISTINCT source_publication FROM "
                    f"public.{s.live_table} WHERE {s.period_col} = %s", (y,))
        got = {r[0] for r in cur.fetchall()}
        if got != {want}:
            bad.append(f"{y}: live source_publication {sorted(map(str, got))[:2]}"
                       f" is not uniformly the tip's {str(want)[:60]!r}")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{len(periods)} taxbase year(s): every live source_publication is "
        "the tip's (as-loaded wrapper removed), and every period's tip names "
        "its file date")


def _private_dicts(path):
    """Problems: a Barnsley/Sheffield code in a string constant other than a
    docstring (a private recode dict would hold one)."""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    doc = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef,
                             ast.AsyncFunctionDef)):
            d = ast.get_docstring(node, clean=False)
            if d is not None and node.body and isinstance(
                    node.body[0], ast.Expr):
                doc.add(id(node.body[0].value))
    pat = re.compile(r"E080000(16|19|38|39)(?![0-9])")
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in doc and pat.search(node.value):
            bad.append(f"line {node.lineno}: {node.value[:40]!r}")
    return bad


def real_codes(cur, specs=None):
    specs = specs or REAL
    _need(cur, specs)
    cur.execute("SELECT lad24cd FROM public.la_boundaries")
    valid = {r[0] for r in cur.fetchall()}
    bad, seen = [], 0
    for s in specs:
        for table in (s.live_table, s.editions_table):
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
        if not _live_periods(cur, s):
            bad.append(f"{s.live_table}: {EMPTY}")
    t6 = specs[2]
    for table in (t6.live_table, t6.editions_table):
        cur.execute(f"""SELECT COUNT(*) FROM public.{table} WHERE
            mapping_status NOT IN ('direct', 'resolved_via_lookup',
                                   'unmapped')
            OR (lad24cd IS NULL) <> (mapping_status = 'unmapped')
            OR (mapping_status = 'direct'
                AND lad24cd IS DISTINCT FROM published_la_code)
            OR (mapping_status = 'resolved_via_lookup'
                AND (lad24cd IS NULL OR lad24cd = published_la_code
                     OR NOT EXISTS (SELECT 1 FROM public.la_code_lookup l
                          WHERE l.old_code = published_la_code
                            AND l.new_code = lad24cd)))""")
        n = cur.fetchone()[0]
        if n:
            bad.append(f"{table}: {n} row(s) whose mapping_status and "
                       "lad24cd disagree (lad24cd is NULL only when "
                       "unmapped)")
    srcs = _private_dicts(HERE / "s22_ctb_editions.py")
    if srcs:
        bad.append(f"private code table in the loader: {srcs[:2]}")
    decl = geography.DATASET_FORM.get(m.RUN_SOURCE, ("?",))[0]
    if decl != "mixed":
        bad.append(f"source 22 is declared {decl!r}, expected 'mixed'")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{seen} distinct codes over the six tables all in la_boundaries; "
        "no E08000038/39 in lad24cd; 615 mapping_status consistent with "
        "lad24cd (NULL only when unmapped); source 22 declared 'mixed'; no "
        "private dict in the loader")


def real_row_counts(cur, specs=None, n=296):
    specs = specs or REAL
    _need(cur, specs)
    main, cls = specs[0], specs[1]
    years = _live_periods(cur, main)
    if not years:
        return False, EMPTY
    bad = []
    for y in years:
        tip = _tip(cur, main, y)
        for table, extra, want in (
                (main.live_table, "", n), (cls.live_table, "", 11 * n),
                (main.editions_table, f" AND edition = {tip}", n),
                (cls.editions_table, f" AND edition = {_tip(cur, cls, y)}",
                 11 * n)):
            cur.execute(f"SELECT COUNT(*) FROM public.{table} WHERE "
                        f"{main.period_col} = %s{extra}", (y,))
            got = cur.fetchone()[0]
            if got != want:
                bad.append(f"{table} {y}: {got} rows, expected {want}")
    cur.execute(f"SELECT COUNT(DISTINCT exemption_class) FROM "
                f"public.{cls.live_table}")
    if cur.fetchone()[0] != 11:
        bad.append("the class table does not hold the 11 unoccupied "
                   "classes")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{len(years)} taxbase year(s): {n:,} main rows and {11 * n:,} "
        "class rows in live and in the tip editions")


REASON_OK = {"suppressed_or_not_available", "not_applicable",
             m.BUILT_FROM_NULL}


def _cols_of(spec):
    return [c for c, _ in spec.value_cols]


def real_null_vs_zero(cur, specs=None):
    """In the live tables and the tip editions: a NULL value carries a
    `column=reason` entry in null_reasons (an edition 1 'as loaded' is the
    exception: the old build recorded no reasons), a stored number never
    does, every reason is a known one, and a 0 is kept as 0."""
    specs = specs or REAL
    _need(cur, specs)
    bad, nulls, zeros, exempt = [], 0, 0, 0
    for s in specs:
        cols = _cols_of(s)
        periods = _live_periods(cur, s)
        if not periods:
            return False, f"{s.live_table}: {EMPTY}"
        tips = {y: _tip(cur, s, y) for y in periods}
        sources = [(s.live_table, "true", False)] + [
            (s.editions_table, f"{s.period_col} = {y} AND edition = {t}",
             True) for y, t in tips.items()]
        for table, where, is_ed in sources:
            sel = ", ".join(cols)
            lab = ", release_label" if is_ed else ", NULL"
            cur.execute(f"SELECT {sel}, null_reasons{lab} FROM "
                        f"public.{table} WHERE {where}")
            for row in cur.fetchall():
                vals, reasons, label = row[:len(cols)], row[-2], row[-1]
                have = {}
                for item in (reasons or "").split(";"):
                    if item:
                        col, _, why = item.partition("=")
                        have[col] = why
                for c, v in zip(cols, vals):
                    if v == 0:
                        zeros += 1  # not a source value
                    if v is None:
                        nulls += 1  # not a source value
                        if c not in have:
                            if label == core.AS_LOADED_LABEL or \
                                    (not is_ed and reasons is None):
                                exempt += 1  # not a source value
                            else:
                                bad.append(f"{table}: {c} is NULL without a "
                                           "reason")
                    elif c in have:
                        bad.append(f"{table}: {c} holds {v} but has the "
                                   f"reason {have[c]!r}")
                for c, why in have.items():
                    if c not in cols or why not in REASON_OK:
                        bad.append(f"{table}: unknown null_reasons entry "
                                   f"{c}={why}")
    return not bad, "; ".join(sorted(set(bad))[:3]) if bad else (
        f"{nulls} NULL and {zeros} zero value cells in live and the tip "
        f"editions: every NULL has its reason ({exempt} in an as-loaded "
        "edition 1 or its live rows, which carry none), no number has one, "
        "reasons are all known")


def _derived_problems(cur, main_t, main_w, cls_t, cls_w, label):
    """Problems with the two derived columns of one main table (with a
    WHERE on period and edition) against the matching class rows."""
    cur.execute(f"""
        SELECT COUNT(*) FROM public.{main_t} e WHERE {main_w} AND
          empty_under_6_months IS DISTINCT FROM (CASE WHEN empty_total IS NOT
            NULL AND empty_6_months_plus IS NOT NULL
            THEN empty_total - empty_6_months_plus END)""")
    a = cur.fetchone()[0]
    cur.execute(f"""
        SELECT COUNT(*) FROM public.{main_t} e WHERE {main_w} AND
          unoccupied_exemptions_total IS DISTINCT FROM (
            SELECT CASE WHEN COUNT(*) = 11 AND COUNT(c.dwellings) = 11
                   THEN SUM(c.dwellings)::integer END
            FROM public.{cls_t} c WHERE c.lad24cd = e.lad24cd AND
                 c.taxbase_year = e.taxbase_year AND {cls_w})""")
    b = cur.fetchone()[0]
    out = []
    if a:
        out.append(f"{label}: {a} row(s) whose empty_under_6_months is not "
                   "empty_total - empty_6_months_plus (NULL if a part is "
                   "NULL)")
    if b:
        out.append(f"{label}: {b} row(s) whose unoccupied_exemptions_total "
                   "is not the sum of its 11 classes (NULL unless all 11 "
                   "are published)")
    return out


def real_derived(cur, specs=None):
    specs = specs or REAL
    _need(cur, specs)
    main, cls = specs[0], specs[1]
    years = _live_periods(cur, main)
    if not years:
        return False, EMPTY
    bad = _derived_problems(cur, main.live_table, "true", cls.live_table,
                            "true", "live")
    n_null = 0
    for y in years:
        tm, tc = _tip(cur, main, y), _tip(cur, cls, y)
        bad += _derived_problems(
            cur, main.editions_table, f"e.taxbase_year = {y} AND e.edition "
            f"= {tm}", cls.editions_table, f"c.edition = {tc}",
            f"{y} ed{tm}")
    cur.execute(f"SELECT COUNT(*) FROM public.{main.live_table} WHERE "
                "unoccupied_exemptions_total IS NULL OR "
                "empty_under_6_months IS NULL")
    n_null = cur.fetchone()[0]
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{len(years)} taxbase year(s): empty_under_6_months and "
        "unoccupied_exemptions_total equal the arithmetic and are NULL "
        f"where a part is NULL, in live and the tip editions "
        f"({n_null} live row(s) NULL)")


def real_cross_615_ctb(cur, specs=None):
    """615 all vacants equals CTB empty_total + unoccupied_exemptions_total
    for every authority, for the latest taxbase year that Table 615 also
    holds, in live and in the tip editions."""
    specs = specs or REAL
    _need(cur, specs)
    main, t6 = specs[0], specs[2]
    cy = set(_live_periods(cur, main))
    ty = set(_live_periods(cur, t6))
    common = sorted(cy & ty)
    if not common:
        return False, ("no taxbase year is held in both the CTB and Table "
                       "615 to compare")
    y = common[-1]
    bad, n = [], 0
    for which in ("live", "tip"):
        if which == "live":
            wm, w6, tm_, t6_ = "true", "true", main.live_table, t6.live_table
        else:
            wm = f"edition = {_tip(cur, main, y)}"
            w6 = f"edition = {_tip(cur, t6, y)}"
            tm_, t6_ = main.editions_table, t6.editions_table
        cur.execute(f"SELECT lad24cd, taxbase_year, empty_total, "
                    f"unoccupied_exemptions_total FROM public.{tm_} WHERE "
                    f"taxbase_year = %s AND {wm}", (y,))
        ctb = [dict(zip(("lad24cd", "taxbase_year", "empty_total",
                         "unoccupied_exemptions_total"), r))
               for r in cur.fetchall()]
        cur.execute(f"SELECT lad24cd, year, vacant_dwellings FROM "
                    f"public.{t6_} WHERE year = %s AND lad24cd IS NOT NULL "
                    f"AND {w6}", (y,))
        t615 = [dict(zip(("lad24cd", "year", "vacant_dwellings"), r))
                for r in cur.fetchall()]
        n = len(ctb)
        problems = m.cross_check_615_ctb(t615, ctb, y)
        bad += [f"{which}: {p}" for p in problems[:2]]
        if not ctb:
            bad.append(f"{which}: no CTB rows for {y}")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{y}: Table 615 all vacants equals CTB empty_total + "
        f"unoccupied_exemptions_total for {n} of {n} authorities in live "
        "and in the tip editions")


# ---------------------------------------------------------------- seeding

def seeded_ctb(e):
    e.seed_ctb(e.cur)


def seeded_all(e):
    e.seed_ctb(e.cur)
    e.seed_615(e.cur, (2023, 2024, 2025))


# ---------------------------------------------------------------- gates 2-4

def _plant_drift(cur, spec, sql, args=()):
    cur.execute("SAVEPOINT plant")
    cur.execute(sql, args)


def _undo_plant(cur):
    cur.execute("ROLLBACK TO SAVEPOINT plant")
    cur.execute("RELEASE SAVEPOINT plant")


def seeded_drift_gate(cur, real_fn, plants, setup=seeded_all, **kw):
    """A seeded complete state passes real_fn(cur, ZZ); each planted drift
    (sql, args, needle) makes it fail naming the needle. Returns
    (ok, detail)."""
    out = {}

    def body(e):
        setup(e)
        out["base"] = real_fn(e.cur, ZZ, **kw)
        miss = []
        for sql, args, needle in plants:
            _plant_drift(e.cur, None, sql, args)
            try:
                ok, detail = real_fn(e.cur, ZZ, **kw)
            except (psycopg2.Error, ValueError, LookupError) as ex:
                ok, detail = False, str(ex)
            _undo_plant(e.cur)
            if ok or needle not in detail:
                miss.append(f"{needle!r}: {detail[:80]}")
        out["miss"] = miss
    scenario(cur, body)
    ok = out["base"][0] and not out["miss"]
    return ok, (out["base"][1] if not out["base"][0] else
                "; ".join(out["miss"]) if out["miss"] else
                f"{len(plants)} planted drifts each caught")


def gate_2_edition1_and_latest(cur):
    name = ("every live period has edition 1; the latest edition equals "
            "live cell for cell")
    plants = [
        (f"UPDATE public.{Z_CTB.live_table} SET second_homes = second_homes "
         "+ 1 WHERE lad24cd = 'E06000001'", (), "edition-only"),
        (f"UPDATE public.{Z_615.live_table} SET long_term_vacant_dwellings "
         "= 1 WHERE published_la_code = 'E06000002' AND year = 2024", (),
         "edition-only"),
        (f"UPDATE public.{Z_CLS.live_table} SET dwellings = dwellings + 1 "
         "WHERE lad24cd = 'E06000001' AND exemption_class = 'K'", (),
         "edition-only"),
        (f"UPDATE public.{Z_615.live_table} SET year = 2030 WHERE year = "
         "2023", (), "no edition 1"),
    ]
    ok, detail = seeded_drift_gate(cur, real_edition1_and_latest, plants)
    mixed(2, name, ok, "seeded complete state passes; " + detail if ok
          else detail, cur, real_edition1_and_latest)


def gate_3_pair_tips(cur):
    name = "CTB main and classes tips carry the same edition number per year"

    def plant(cur):
        pass
    out = {}

    def body(e):
        seeded_ctb(e)
        out["base"] = real_pair_tips(e.cur, ZZ)
        recs = m.records(e.cur, Z_CLS, "2025", 1)
        recs[0]["dwellings"] = 999
        core.insert_edition(e.cur, Z_CLS, recs, "2025",
                            release_label="planted", published_date=None,
                            source_file="x", source_sha256="planted",
                            supersedes=1)
        out["planted"] = real_pair_tips(e.cur, ZZ)
    scenario(cur, body)
    ok = (out["base"][0] and not out["planted"][0]
          and "classes tip edition 2" in out["planted"][1])
    mixed(3, name, ok, "equal tips pass; a classes-only edition is caught"
          if ok else f"{out}", cur, real_pair_tips)


def gate_4_source_uniform(cur):
    name = ("live source_publication uniform per year and equal to the "
            "tip's; every tip names its file date")
    plants = [
        (f"UPDATE public.{Z_CTB.live_table} SET source_publication = 'other'"
         " WHERE lad24cd = 'E06000002'", (), "not uniformly"),
        (f"UPDATE public.{Z_CTB.live_table} SET source_publication = "
         "'other'", (), "not uniformly"),
    ]
    ok, detail = seeded_drift_gate(cur, real_source_uniform, plants)
    mixed(4, name, ok, detail, cur, real_source_uniform)


# ---------------------------------------------------------------- gate 5

def gate_5_codes(cur):
    name = ("codes: CTB and classes in la_boundaries, no E08000038/39 in "
            "lad24cd, 615 mapping_status consistent with lad24cd, no "
            "private recode dict")
    out = {}

    def body(e):
        seeded_all(e)
        out["base"] = real_codes(e.cur, ZZ)
        e.cur.execute(f"SELECT published_la_code, lad24cd, mapping_status "
                      f"FROM public.{Z_615.live_table} WHERE year = 2025 "
                      "AND published_la_code IN ('E08000038', 'E08000016')")
        out["barnsley"] = e.cur.fetchall()
        miss = []
        for sql, needle in (
            (f"UPDATE public.{Z_CTB.live_table} SET lad24cd = 'E08000038' "
             "WHERE lad24cd = 'E08000016'", "Barnsley/Sheffield"),
            (f"UPDATE public.{Z_615.live_table} SET lad24cd = 'E08000039' "
             "WHERE published_la_code = 'E08000038' AND year = 2025",
             "Barnsley/Sheffield"),
            (f"UPDATE public.{Z_615.live_table} SET lad24cd = NULL WHERE "
             "published_la_code = 'E06000001' AND year = 2024",
             "mapping_status and lad24cd disagree"),
            (f"UPDATE public.{Z_615.live_table} SET mapping_status = "
             "'unmapped' WHERE published_la_code = 'E06000001' AND year = "
             "2024", "mapping_status and lad24cd disagree"),
            (f"UPDATE public.{Z_615.live_table} SET lad24cd = 'E06000099' "
             "WHERE published_la_code = 'E06000001' AND year = 2024",
             "mapping_status and lad24cd disagree"),
            (f"UPDATE public.{Z_CLS.live_table} SET lad24cd = 'E06999999' "
             "WHERE lad24cd = 'E06000001'", "not in la_boundaries"),
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
    scenario(cur, body)
    # the AST check: a planted dict is caught, a docstring mention is not
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "x.py"
        p.write_text('"""E08000038 in a docstring"""\nX = {"a": '
                     '"E08000038"}\n', encoding="utf-8")
        ast_bad = _private_dicts(p)
    ast_ok = len(ast_bad) == 1 and "line 2" in ast_bad[0]
    ok = (out["base"][0] and not out["miss"] and ast_ok
          and ("E08000038", "E08000016", "resolved_via_lookup")
          in out["barnsley"])
    detail = ("Barnsley's 2025 code E08000038 resolved to E08000016 "
              "(resolved_via_lookup) with no E08000038/39 in lad24cd; six "
              "planted breaks each caught; a private dict is caught by the "
              "AST check, a docstring is not" if ok else
              f"seeded: {out.get('base')} {out.get('miss')} ast {ast_bad}")
    mixed(5, name, ok, detail, cur, real_codes)


# ---------------------------------------------------------------- gate 6

def gate_6_row_counts(cur):
    name = ("296 CTB rows and 3,256 class rows per year (11 classes x "
            "authorities) in live and the tip editions")
    out = {}

    def body(e):
        seeded_ctb(e)
        out["base"] = real_row_counts(e.cur, ZZ, n=e.n_auth)
        out["wrong_n"] = real_row_counts(e.cur, ZZ, n=e.n_auth + 1)
        e.cur.execute(f"DELETE FROM public.{Z_CLS.live_table} WHERE "
                      "lad24cd = 'E06000001' AND exemption_class = 'K'")
        out["short"] = real_row_counts(e.cur, ZZ, n=e.n_auth)
    scenario(cur, body)
    ok = (out["base"][0] and not out["wrong_n"][0] and not out["short"][0])
    mixed(6, name, ok, ("the expected count passes; one more authority or a "
                        "missing class row fails" if ok else f"{out}"),
          cur, real_row_counts)


# ---------------------------------------------------------------- gate 7

def _cell_file(e):
    """A CTB workbook with a [x] or [z] in every published column and every
    class, and a 0 in each at another authority."""
    codes = e.codes
    cols = list(m.CTB_PUBLISHED) + [f"class {k}"
                                    for k in m.UNOCCUPIED_CLASSES]
    values, marks, zeros = {}, {}, {}
    for k, col in enumerate(cols):
        a, z = codes[k % len(codes)], codes[(k + 2) % len(codes)]
        mark = "[x]" if k % 2 == 0 else "[z]"
        values.setdefault(a, {})[col] = mark
        values.setdefault(z, {})[col] = 0
        if col == "empty_total":
            values[z]["empty_6_months_plus"] = 0
        marks[col], zeros[col] = (a, mark), z
    return values, marks, zeros


def seeded_null_vs_zero(cur):
    problems, out = [], {}

    def body(e):
        values, marks, zeros = _cell_file(e)
        e.ctb(values=values)
        rc, text, _ = e.cmd(["load", "--commit"])
        if rc != 0:
            problems.append(f"load failed: {text[-150:]}")
            return
        def res(code):
            return "E08000016" if code == "E08000038" else code
        for table, tag in ((Z_CTB.live_table, "live"),
                           (Z_CTB.editions_table, "edition")):
            for col in m.CTB_PUBLISHED:
                a, mark = marks[col]
                z = zeros[col]
                e.cur.execute(f"SELECT {col}, null_reasons FROM public."
                              f"{table} WHERE lad24cd = %s", (res(a),))
                v, why = e.cur.fetchone()
                e.cur.execute(f"SELECT {col}, null_reasons FROM public."
                              f"{table} WHERE lad24cd = %s", (res(z),))
                v0, why0 = e.cur.fetchone()
                reason = ("suppressed_or_not_available" if mark == "[x]"
                          else "not_applicable")
                if v is not None or f"{col}={reason}" not in (why or ""):
                    problems.append(f"{tag} {col}: {mark} stored as {v!r} "
                                    f"with {why!r}")
                if v0 != 0 or f"{col}=" in (why0 or ""):
                    problems.append(f"{tag} {col}: a published 0 stored as "
                                    f"{v0!r} with {why0!r}")
        for table in (Z_CLS.live_table, Z_CLS.editions_table):
            for k in m.UNOCCUPIED_CLASSES:
                col = f"class {k}"
                a, mark = marks[col]
                z = zeros[col]
                e.cur.execute(f"SELECT dwellings, null_reasons FROM public."
                              f"{table} WHERE lad24cd = %s AND "
                              "exemption_class = %s",
                              (res(a), k))
                v, why = e.cur.fetchone()
                e.cur.execute(f"SELECT dwellings, null_reasons FROM public."
                              f"{table} WHERE lad24cd = %s AND "
                              "exemption_class = %s",
                              (res(z), k))
                v0, why0 = e.cur.fetchone()
                reason = ("suppressed_or_not_available" if mark == "[x]"
                          else "not_applicable")
                if v is not None or why != f"dwellings={reason}":
                    problems.append(f"{table} class {k}: {mark} stored as "
                                    f"{v!r} with {why!r}")
                if v0 != 0 or why0 is not None:
                    problems.append(f"{table} class {k}: a published 0 "
                                    f"stored as {v0!r} with {why0!r}")
        # 615: [x] and [z] on one sheet, a 0 on both
        c = e.codes615
        e.t615((2025,), values={
            ("All_vacants", c[0], 2025): "[x]",
            ("All_long_term_vacants", c[1], 2025): "[z]",
            ("All_vacants", c[2], 2025): 0,
            ("All_long_term_vacants", c[2], 2025): 0})
        rc, text, _ = e.run_main(e.cur, ["load-615", "--commit"],
                                 bounds={c[2], "E08000016"})
        if rc != 0:
            problems.append(f"load-615 failed: {text[-150:]}")
            return
        for table in (Z_615.live_table, Z_615.editions_table):
            e.cur.execute(f"SELECT published_la_code, vacant_dwellings, "
                          "long_term_vacant_dwellings, null_reasons FROM "
                          f"public.{table} WHERE year = 2025 AND "
                          "published_la_code = ANY(%s)", (c[:3],))
            got = {r[0]: r[1:] for r in e.cur.fetchall()}
            if got[c[0]][0] is not None or got[c[0]][2] != (
                    "vacant_dwellings=suppressed_or_not_available"):
                problems.append(f"{table}: [x] vacant stored as {got[c[0]]}")
            if got[c[1]][1] is not None or got[c[1]][2] != (
                    "long_term_vacant_dwellings=not_applicable"):
                problems.append(f"{table}: [z] long-term stored as "
                                f"{got[c[1]]}")
            if got[c[2]] != (0, 0, None):
                problems.append(f"{table}: a published 0 stored as "
                                f"{got[c[2]]}")
        # tokens that must halt, with nothing stored
        before = state(e.cur)
        halted = []
        for bad in ("k", None, "[i]", -1, 1.5, "1,234", "N/A"):
            p = e.ctb(register=False, year=2026,
                      values={e.codes[0]: {"second_homes": bad}})
            ok, _ = e.halts(["load", "--file", str(p), "--commit"], before)
            if not ok:
                halted.append(repr(bad))
        if halted:
            problems.append(f"tokens not halting: {halted}")
        out["checked"] = len(m.CTB_PUBLISHED) + len(m.UNOCCUPIED_CLASSES)
        out["real_fn"] = real_null_vs_zero(e.cur, ZZ)
        if not out["real_fn"][0]:
            problems.append(f"real_null_vs_zero: {out['real_fn'][1]}")
    scenario(cur, body)
    return problems, out


def gate_7_null_vs_zero(cur):
    name = ("NULL versus 0 never conflated: seeded per column ([x] and [z] "
            "are NULL with their reason, a published 0 stays 0, other "
            "tokens halt)")
    problems, out = seeded_null_vs_zero(cur)
    mixed(7, name, not problems,
          f"{out.get('checked', 0)} CTB columns and classes plus both 615 "
          "columns each checked with a [x]/[z] and with a 0, in live and "
          "the editions; k, blank, [i], negative, decimal, thousands "
          "separator and N/A halt" if not problems
          else "; ".join(problems[:3]), cur, real_null_vs_zero)


# ---------------------------------------------------------------- gate 8

def gate_8_derived(cur):
    name = ("derived columns NULL when a part is NULL, equal to the "
            "arithmetic otherwise (live and seeded)")
    out = {"miss": []}

    def body(e):
        c = e.codes
        e.ctb(values={
            c[2]: {"class K": "[x]"}, c[3]: {"class Q": 0},
            c[0]: {"empty_total": "[x]"},
            c[1]: {"empty_6_months_plus": "[x]"},
            c[4]: {"class B": "[z]", "class D": 0}})
        rc, text, _ = e.cmd(["load", "--commit"])
        out["rc"] = rc
        out["base"] = real_derived(e.cur, ZZ)
        e.cur.execute(f"SELECT lad24cd, unoccupied_exemptions_total, "
                      f"empty_under_6_months, null_reasons FROM public."
                      f"{Z_CTB.live_table} ORDER BY 1")
        rows = {r[0]: r[1:] for r in e.cur.fetchall()}
        want_null = {c[2], c[4] if c[4] != "E08000038" else "E08000016"}
        for code in want_null:
            if rows[code][0] is not None or "unoccupied_exemptions_total=" \
                    "built_from_null" not in (rows[code][2] or ""):
                out["miss"].append(f"{code}: total {rows[code]}")
        for code, col in ((c[0], 1), (c[1], 1)):
            if rows[code][col] is not None:
                out["miss"].append(f"{code}: empty_under_6_months "
                                   f"{rows[code]}")
        if rows[c[3]][0] is None:
            out["miss"].append(f"{c[3]}: a class 0 made the total NULL")
        for sql, args, needle in (
            (f"UPDATE public.{Z_CTB.live_table} SET "
             "unoccupied_exemptions_total = 0 WHERE lad24cd = %s", (c[2],),
             "unoccupied_exemptions_total"),
            (f"UPDATE public.{Z_CTB.live_table} SET "
             "empty_under_6_months = 7 WHERE lad24cd = %s", (c[1],),
             "empty_under_6_months"),
            (f"UPDATE public.{Z_CTB.live_table} SET "
             "empty_under_6_months = 0 WHERE lad24cd = %s", (c[0],),
             "empty_under_6_months")):
            e.cur.execute("SAVEPOINT plant")
            e.cur.execute(sql, args)
            ok, detail = real_derived(e.cur, ZZ)
            e.cur.execute("ROLLBACK TO SAVEPOINT plant")
            e.cur.execute("RELEASE SAVEPOINT plant")
            if ok or needle not in detail:
                out["miss"].append(f"{needle}: planted change not caught "
                                   f"({detail[:60]})")
        # a tip edition with a wrong derived column (editions are
        # append-only: plant a new edition)
        e.cur.execute("SAVEPOINT plant")
        recs = m.records(e.cur, Z_CTB, "2025", 1)
        recs[0]["empty_under_6_months"] = (recs[0]["empty_under_6_months"]
                                           or 0) + 3
        core.insert_edition(e.cur, Z_CTB, recs, "2025",
                            release_label="planted", published_date=None,
                            source_file="x", source_sha256="planted",
                            supersedes=1)
        ok, detail = real_derived(e.cur, ZZ)
        e.cur.execute("ROLLBACK TO SAVEPOINT plant")
        e.cur.execute("RELEASE SAVEPOINT plant")
        if ok or "ed2" not in detail:
            out["miss"].append(f"tip edition: not caught ({detail[:60]})")
    scenario(cur, body)
    ok = out.get("rc") == 0 and out["base"][0] and not out["miss"]
    mixed(8, name, ok,
          "a suppressed class makes the 11-class total NULL (built_from_null)"
          ", a published class 0 and a [z] do not corrupt the sum, a "
          "suppressed part makes empty_under_6_months NULL; three planted "
          "changes each caught" if ok else f"{out}", cur, real_derived)


# ---------------------------------------------------------------- gate 9

def gate_9_identity(cur):
    name = ("identity from the file: cover year, block title, header and "
            "date mismatches halt before any write")
    out = {"ok": [], "bad": []}

    def case(label, ok):
        (out["ok"] if ok else out["bad"]).append(label)

    def body(e):
        e.seed_ctb(e.cur)                # the control: a good file loads
        e.seed_615(e.cur, (2023, 2024))
        before = state(e.cur)

        def ctb_file(label, needle=None, argv_extra=(), **kw):
            kw.setdefault("year", 2026)
            p = e.ctb(register=False, **kw)
            ok, text = e.halts(["load", "--file", str(p), *argv_extra,
                                "--commit"], before, needle)
            case(label, ok)

        ctb_file("--release differs from the cover", "the cover says 2026",
                 ["--release", "2025"])
        ctb_file("cover title not a taxbase file", title="Something else")
        ctb_file("block 1.18 retitled", titles={
            "1.18": "Table 1.18. Total number of dwellings, no details"})
        ctb_file("a class column missing", drop_class="K")
        ctb_file("block 1.11 retitled", titles={
            "1.11": "Table 1.11. Number of properties on a list"})

        def header(wb):
            wb["Council Taxbase Data"]["D6"] = "Council"
        p = e.ctb(register=False, year=2026)
        edit_xlsx(p, header)
        ok, _ = e.halts(["load", "--file", str(p), "--commit"], before)
        case("the Local Authority header changed", ok)
        # 615
        for label, kw in (
                ("615 cover end year differs from the header years",
                 {"end": 2030}),
                ("615 date headers out of order",
                 {"dates": ["02/10/2024", "02/10/2023", "06/10/2025"]}),
                ("615 two date headers in one year",
                 {"dates": ["02/10/2023", "03/10/2023", "06/10/2025"]}),
                ("615 cover title not Table 615", {"title": "Something"})):
            p = e.t615((2023, 2024, 2025), register=False, **kw)
            ok, _ = e.halts(["load-615", "--file", str(p), "--commit"],
                            before)
            case(label, ok)
        # the page title is not the release the link says
        e.ctb(year=2026)
        e.api[tl.REL.format(y=2026)]["title"] = "Council Taxbase 2025 in England"
        ok, _ = e.halts(["load", "--commit"], before)
        case("the release page is another year's", ok)
        rc, text, _ = e.cmd(["load", "--file", str(e.ctb(
            register=False, year=2026)), "--release", "2026"])
        out["good"] = rc == 0
    scenario(cur, body)
    ok = not out["bad"] and out.get("good") and len(out["ok"]) >= 10
    report(9, name, ok, f"{len(out['ok'])} seeded identity mismatches each "
           "halted with nothing stored, no commit and no run-log row; a "
           "matching file is accepted" if ok else
           f"did not halt: {out['bad']}; accepted good={out.get('good')}")


# --------------------------------------------------------------- gate 10

def gate_10_reconciliation(cur):
    name = ("reconciliation: the authorities sum to England in every used "
            "column and year; a seeded failure halts")
    out = {"ok": [], "bad": []}

    def body(e):
        e.seed_both()
        before = state(e.cur)
        good = e.ctb(register=False, year=2026)
        out["clean_ctb"] = m.reconcile(m.read_ctb(good)) == []
        out["clean_615"] = m.reconcile(m.read_615(e.t615(
            (2023, 2024), register=False))) == []
        for label, p, argv, needle in (
            ("CTB England second_homes", e.ctb(
                register=False, year=2026, england={"second_homes": 1}),
             "load", "do not sum to England"),
            ("CTB England class D", e.ctb(
                register=False, year=2026, england={"class D": 1}),
             "load", "class D"),
            ("615 England all vacants", e.t615(
                (2025,), register=False,
                england={("All_vacants", 2025): 1}), "load-615",
             "do not sum to England"),
            ("615 England long-term", e.t615(
                (2024,), register=False,
                england={("All_long_term_vacants", 2024): 1}), "load-615",
             "do not sum to England")):
            ok, text = e.halts([argv, "--file", str(p), "--commit"], before,
                               needle)
            (out["ok"] if ok else out["bad"]).append(label)
    scenario(cur, body)
    ok = (out["clean_ctb"] and out["clean_615"] and not out["bad"])
    report(10, name, ok, "good files reconcile; England off by one value in "
           f"{len(out['ok'])} places halts with nothing stored" if ok else
           f"{out}")


# --------------------------------------------------------------- gate 11

def gate_11_cross(cur):
    name = ("615 all vacants equals CTB empty_total + "
            "unoccupied_exemptions_total for every authority")
    out = {}

    def body(e):
        vals = {("All_vacants", c, 2025): 1280 + 120 * i
                for i, c in enumerate(e.codes)}
        e.ctb()
        e.ok(e.cur, ["load", "--commit"])
        e.t615((2025,), values=vals, codes=e.codes + ["E08000016"])
        e.ok(e.cur, ["load-615", "--commit"])
        out["base"] = real_cross_615_ctb(e.cur, ZZ)
        # the pure check on the stored values
        ctb = m.records(e.cur, Z_CTB, "2025")
        t6 = m.records(e.cur, Z_615, "2025")
        out["pure_ok"] = m.cross_check_615_ctb(t6, ctb, 2025) == []
        t6[0]["vacant_dwellings"] += 1
        out["pure_bad"] = len(m.cross_check_615_ctb(t6, ctb, 2025))
        e.cur.execute(f"UPDATE public.{Z_615.live_table} SET "
                      "vacant_dwellings = vacant_dwellings + 1 WHERE "
                      "published_la_code = 'E06000002' AND year = 2025")
        out["live"] = real_cross_615_ctb(e.cur, ZZ)
        e.cur.execute(f"UPDATE public.{Z_615.live_table} SET "
                      "vacant_dwellings = vacant_dwellings - 1 WHERE "
                      "published_la_code = 'E06000002' AND year = 2025")
        e.cur.execute(f"UPDATE public.{Z_CTB.live_table} SET "
                      "unoccupied_exemptions_total = NULL WHERE lad24cd = "
                      "'E06000003'")
        out["null"] = real_cross_615_ctb(e.cur, ZZ)
    scenario(cur, body)
    ok = (out["base"][0] and not out["live"][0] and not out["null"][0]
          and out["pure_ok"] and out["pure_bad"] == 1)
    mixed(11, name, ok, ("the reconciling fixture passes; a one-dwelling "
                         "difference or a NULL on one side fails" if ok
                         else f"{out}"), cur, real_cross_615_ctb)


# --------------------------------------------------------------- gate 12

def gate_12_older_file(cur):
    name = ("older-file guard on the page, --release and --file paths, "
            "ranked by the file's own cover date")
    problems = []

    def ctb_case(e, label, extra, by_file):
        e.seed_ctb(e.cur)                         # revised, dated 2026-01-21
        path = e.ctb(pub=PUB_FIRST, values={e.codes[0]: {"second_homes":
                                                         101}},
                     register=not by_file)
        before = state(e.cur)
        argv = ["load", "--commit", *extra]
        if by_file:
            argv += ["--file", str(path)]
        ok, text = e.halts(argv, before, "older than the tips of all its "
                           "periods")
        if not ok:
            problems.append(f"CTB {label}: {text[-120:]}")

    def body(e):
        for label, extra, by_file in (("page", [], False),
                                      ("--release", ["--release", "2025"],
                                       False), ("--file", [], True)):
            def one(c, label=label, extra=extra, by_file=by_file):
                with env(c) as e2:
                    ctb_case(e2, label, extra, by_file)
            _in_savepoint(e.cur, one)

        def allow(c):
            with env(c) as e2:
                e2.seed_ctb(c)
                path = e2.ctb(pub=PUB_FIRST, values={e2.codes[0]: {
                    "second_homes": 101}}, register=False)
                rc, text, logged = e2.cmd(["load", "--commit", "--file",
                                           str(path), "--allow-older-file"])
                if rc != 0 or tl.editions(c, tl.ZZ_CTB, "2025") != [
                        (1, None, e2.n_auth), (2, 1, e2.n_auth)] or \
                        "--allow-older-file given" not in logged.call_args[
                            0][2]:
                    problems.append(f"--allow-older-file: {text[-120:]}")
        _in_savepoint(e.cur, allow)

        def equal_rank(c):
            with env(c) as e2:
                e2.seed_ctb(c)
                e2.ctb(values={e2.codes[0]: {"second_homes": 101}})
                ok, text = e2.halts(["load", "--commit"], state(c),
                                    "two files claim the same date")
                if not ok:
                    problems.append(f"equal rank: {text[-120:]}")
        _in_savepoint(e.cur, equal_rank)

        def t615_case(c, by_file, extra=()):
            with env(c) as e2:
                e2.seed_615(c, (2023, 2024, 2025))
                p = e2.t615((2023, 2024), latest="January 2026",
                            register=not by_file,
                            values={("All_vacants", e2.codes615[0], 2024): 1})
                argv = ["load-615", "--commit", *extra]
                if by_file:
                    argv += ["--file", str(p)]
                ok, text = e2.halts(argv, state(c), "older than the tips")
                if not ok:
                    problems.append(f"615 {'--file' if by_file else 'page'}:"
                                    f" {text[-120:]}")
        _in_savepoint(e.cur, lambda c: t615_case(c, False))
        _in_savepoint(e.cur, lambda c: t615_case(c, True))

        # the rank is the cover's, never the link or the file name
        def by_name(c):
            with env(c) as e2:
                e2.seed_ctb(c)
                e2.ctb(pub=PUB_FIRST, values={e2.codes[0]: {
                    "second_homes": 101}})
                e2.urls = {u.replace("/c2/", "/c9999/"): p
                           for u, p in e2.urls.items()}
                e2.api[tl.REL.format(y=2025)] = tl.release_page(2025, [(
                    "Council Taxbase: Local authority level data for 2025 "
                    "(revised)", "https://assets.example/media/c9999/"
                    "2099_LA.xlsx")])
                e2.urls["https://assets.example/media/c9999/2099_LA.xlsx"] \
                    = list(e2.urls.values())[-1]
                ok, text = e2.halts(["load", "--commit"], state(c),
                                    "older than the tips")
                if not ok:
                    problems.append(f"a later-numbered, later-named older "
                                    f"file: {text[-120:]}")
        _in_savepoint(e.cur, by_name)
    scenario(cur, body)
    report(12, name, not problems,
           "an older CTB file (cover 6 November 2025 over a held 21 January "
           "2026) is skipped and halts on the page, --release and --file "
           "paths with a later link and file name; an older 615 file (Latest "
           "Update January 2026 over 25 June 2026) halts on the page and "
           "--file paths; equal date with different content stops; "
           "--allow-older-file overrides and is logged" if not problems
           else "; ".join(problems[:3]))


# --------------------------------------------------------------- gate 13

def gate_13_stop_conditions(cur):
    name = ("stop conditions: each seeded REJECTED, stores nothing and "
            "writes no ledger row; the limits themselves pass")
    problems, done = [], []

    def ctb_stop(label, needle, **kw):
        def body(c):
            with env(c, big=True) as e:
                e.seed_ctb(c, pub=PUB_FIRST)
                e.ctb(pub=PUB_REVISED, **kw.get("file", {}))
                before = state(c)
                argv = ["load", "--commit", *kw.get("argv", [])]
                ok, text = e.halts(argv, before, needle)
                ok = ok and "REJECTED" in text
                (done if ok else problems).append(
                    label if ok else f"{label}: {text[-140:]}")
        _in_savepoint(cur, body)

    def t615_stop(label, needle, years, setup_kw, new_year=False, **kw):
        def body(c):
            with env(c, big=True) as e:
                e.seed_615(c, (2023, 2024))
                e.t615(years, latest="26 June 2026", **setup_kw)
                before = state(c)
                ok, text = e.halts(["load-615", "--commit"], before, needle,
                                   **kw)
                ok = ok and "REJECTED" in text
                (done if ok else problems).append(
                    label if ok else f"{label}: {text[-140:]}")
        _in_savepoint(cur, body)

    # the big env needs the seed's codes: read them once for the cases
    with env(cur, big=True) as probe:
        codes = probe.codes
        c615 = probe.codes615
    ctb_stop("authority count", "authorities, expected",
             file={"codes": codes[:-1]})
    ctb_stop("national total", "national second_homes",
             file={"values": {codes[0]: {"second_homes": 100 + 400}}})
    ctb_stop("31 authorities changed", "31 authorities changed",
             file={"values": {c: {"second_homes": 100 + i + 1}
                              for i, c in enumerate(codes[:31])}})
    ctb_stop("total_dwellings by 10%", "total_dwellings moves by more",
             file={"values": {codes[1]: {"total_dwellings": 70000}}})

    def zero_null(c):
        with env(c, big=True) as e:
            e.seed_ctb(c, pub=PUB_FIRST, values={codes[2]: {"class K": 0}})
            e.ctb(values={codes[2]: {"class K": "[x]"}})
            before = state(c)
            ok, text = e.halts(["load", "--commit"], before,
                               "--acknowledge 2025")
            (done if ok and "REJECTED" in text else problems).append(
                "0 to NULL" if ok else f"0 to NULL: {text[-140:]}")
            rc, text, logged = e.cmd(["load", "--acknowledge", "2025",
                                      "--commit"])
            if rc != 0 or "ACKNOWLEDGED" not in logged.call_args[0][2]:
                problems.append(f"--acknowledge did not release the 0 to "
                                f"NULL: {text[-100:]}")
    _in_savepoint(cur, zero_null)

    def limit(c):
        with env(c, big=True) as e:
            e.seed_ctb(c, pub=PUB_FIRST)
            e.ctb(pub=PUB_REVISED, values={
                cd: {"second_homes": 100 + i + 1}
                for i, cd in enumerate(codes[:30])})
            rc, text, _ = e.cmd(["load", "--commit"])
            if rc != 0:
                problems.append(f"30 authorities changed (the limit) "
                                f"rejected: {text[-120:]}")
            else:
                done.append("30 authorities changed passes")
    _in_savepoint(cur, limit)

    t615_stop("615 England total", "England vacant_dwellings", (2023,),
              {"values": {("All_vacants", c615[0], 2023): 1500}})
    t615_stop("615 many authorities by 25%", "authorities change by more",
              (2023,),
              {"values": {("All_vacants", c, 2023): 700 for c in c615[:12]}})
    t615_stop("615 code appearing", "appearing", (2023,),
              {"codes": c615 + [probe_extra(cur)]})
    t615_stop("615 code disappearing", "disappearing", (2023,),
              {"codes": c615[1:]})
    t615_stop("615 new year without an authority", "without a number",
              (2025,), {}, bounds=set(codes) | {"E06999999"})
    report(13, name, not problems, f"{len(done)} cases: " + "; ".join(done)
           + " each REJECTED with exit 1, nothing stored and no ledger row"
           if not problems else "; ".join(problems[:3]))


def probe_extra(cur):
    return big_codes(cur)[40]


# --------------------------------------------------------------- gate 14

def gate_14_one_transaction(cur):
    name = ("a new period in one transaction (CTB: main edition, classes "
            "edition, live rows, ledger row); a failing write rolls all "
            "back")
    problems = []

    def new_year(e):
        e.ctb()
        rc, text, logged = e.cmd(["load", "--commit"])
        n = e.n_auth
        led = tl.ledger(e.cur, tl.L_CTB)
        got = (tl.editions(e.cur, Z_CTB, "2025"),
               tl.editions(e.cur, Z_CLS, "2025"),
               tl.count(e.cur, Z_CTB.live_table),
               tl.count(e.cur, Z_CLS.live_table), len(led))
        if rc != 0 or got != ([(1, None, n)], [(1, None, 11 * n)], n,
                              11 * n, 1) or led[0][1:] != ("new", 1):
            problems.append(f"new year: {got} {led} {text[-100:]}")
        e.cur.execute(f"SELECT COUNT(DISTINCT source_sha256) FROM (SELECT "
                      f"source_sha256 FROM public.{Z_CTB.editions_table} "
                      f"UNION ALL SELECT source_sha256 FROM public."
                      f"{Z_CLS.editions_table}) q")
        if e.cur.fetchone()[0] != 1:
            problems.append("main and classes editions do not share their "
                            "pair hash")

    def failing(label, patch, check):
        def body(c):
            with env(c) as e:
                e.ctb()
                with patch:
                    rc, text, logged = e.cmd(["load", "--commit"])
                zero = (tl.count(c, Z_CTB.live_table) == 0
                        and tl.count(c, Z_CLS.live_table) == 0
                        and tl.count(c, Z_CTB.editions_table) == 0
                        and tl.count(c, Z_CLS.editions_table) == 0
                        and tl.count(c, tl.L_CTB) == 0)
                if rc != 1 or not zero or logged.called or \
                        "2025: FAILED" not in text:
                    problems.append(f"{label}: rc {rc} zero={zero}: "
                                    f"{text[-100:]}")
        _in_savepoint(cur, body)

    real_ins, real_rec = m.insert_live, pe.record_file_check

    def ins(c, profile, period, records):
        if profile.spec.name == "zz_s22_cls":
            raise RuntimeError("boom")
        return real_ins(c, profile, period, records)

    def rec(*a, **k):
        raise RuntimeError("boom")
    scenario(cur, new_year)
    failing("a failing classes insert", mock.patch.object(
        m, "insert_live", side_effect=ins), None)
    failing("a failing ledger write", mock.patch.object(
        pe, "record_file_check", side_effect=rec), None)

    def t615(e):
        e.t615((2024, 2025))
        with mock.patch.object(pe, "record_file_check", side_effect=rec):
            rc, text, _ = e.cmd(["load-615", "--commit"])
        if rc != 1 or tl.count(e.cur, Z_615.editions_table) or \
                tl.count(e.cur, Z_615.live_table) or \
                tl.count(e.cur, tl.L_615):
            problems.append(f"615 failing ledger write: {text[-100:]}")
        rc, text, _ = e.cmd(["load-615", "--commit"])
        if rc != 0 or len(tl.ledger(e.cur, tl.L_615)) != 2:
            problems.append(f"615 rerun: {text[-100:]}")
    scenario(cur, t615)
    report(14, name, not problems,
           "edition 1 (main and classes), live rows and one ledger row "
           "stored together with one pair hash; a failing classes insert "
           "and a failing ledger write each roll all of them back, and a "
           "failing 615 ledger write rolls its years back" if not problems
           else "; ".join(problems[:3]))


# --------------------------------------------------------------- gate 15

def gate_15_ledger(cur):
    name = ("ledger skip needs the (URL, sha256) pair for every period the "
            "file covers")
    problems = []

    def body(e):
        # CTB: the same file again is not parsed
        e.seed_ctb(e.cur)
        calls = []
        real = m.read_ctb
        with mock.patch.object(m, "read_ctb", side_effect=lambda p: (
                calls.append(p), real(p))[1]):
            rc, text, logged = e.cmd(["load", "--commit"])
        if calls or "nothing parsed" not in text or logged.called:
            problems.append(f"CTB known pair was parsed: {text[-100:]}")
        # a changed file under the same URL is read
        e.ctb(pub=PUB_REVISED, values={e.codes[0]: {"second_homes": 777}})
        calls.clear()
        with mock.patch.object(m, "read_ctb", side_effect=lambda p: (
                calls.append(p), real(p))[1]):
            rc, text, _ = e.cmd(["load"])
        if len(calls) != 1:
            problems.append("a changed sha256 was not read")
        # 615: held for 2023-2024 only, file of 2023-2025 with 2025
        # rejected -> read again on the rerun
        e.seed_615(e.cur, (2023, 2024))
        e.t615((2023, 2024, 2025), latest="26 June 2026")
        rc, text, logged = e.cmd(["load-615", "--commit"],
                                 bounds=e.bounds | {"E06999999"})
        if rc != 1 or "2025: REJECTED" not in text:
            problems.append(f"615 partial run: {text[-100:]}")
        calls615 = []
        real6 = m.read_615
        with mock.patch.object(m, "read_615", side_effect=lambda p: (
                calls615.append(p), real6(p))[1]):
            rc, text, _ = e.cmd(["load-615", "--commit"],
                                bounds=e.bounds | {"E06999999"})
        if len(calls615) != 1 or "nothing parsed" in text:
            problems.append("a 615 file whose pair is held for two of three "
                            "years was skipped")
        # then the whole pair is held
        rc, text, _ = e.cmd(["load-615", "--commit"])
        calls615.clear()
        with mock.patch.object(m, "read_615", side_effect=lambda p: (
                calls615.append(p), real6(p))[1]):
            rc, text, _ = e.cmd(["load-615", "--commit"])
        if calls615 or "nothing parsed" not in text:
            problems.append(f"the full pair was parsed again: {text[-100:]}")
        # the engine functions directly
        sha, where = "a" * 64, "https://x/f.ods"
        src = m.ledger_source(where, m.date(2026, 6, 25), [2023, 2024, 2025])
        ranks = {2023: m.date(2026, 6, 25), 2024: m.date(2026, 6, 25),
                 2025: m.date(2026, 6, 25)}
        full = {y: {(src, sha)} for y in (2023, 2024, 2025)}
        part = {y: {(src, sha)} for y in (2023, 2024)}
        if m.ledger_complete(full, where, sha, ranks) != [2023, 2024, 2025] \
                or m.ledger_complete(part, where, sha, ranks) is not None \
                or m.ledger_complete(full, where, "b" * 64, ranks) is not None \
                or m.ledger_complete(full, "https://x/other.ods", sha,
                                     ranks) is not None \
                or m.ledger_complete(full, where, sha, ranks,
                                     stranded=[2024]) is not None:
            problems.append("ledger_complete accepted an incomplete pair, a "
                            "changed sha, another URL or a stranded period")
    scenario(cur, body)
    report(15, name, not problems,
           "a known CTB pair is not parsed, a changed sha256 is; a 615 file "
           "held for two of its three years is read again until the third "
           "is stored, then skipped; ledger_complete refuses a partial "
           "pair, another sha256 or URL and a stranded period"
           if not problems else "; ".join(problems[:3]))


# --------------------------------------------------------------- gate 16

def gate_16_preview(cur):
    name = ("preview and --simulate write nothing (editions, ledgers, live, "
            "run log), and no commit")
    problems = []

    def check(e, label, argv):
        before = state(e.cur)
        rc, text, logged = e.cmd(argv)
        if rc not in (0, 1) or state(e.cur) != before or logged.called \
                or e.commits():
            problems.append(f"{label}: rc {rc}, state changed or logged")
        return text

    def body(e):
        e.seed_ctb(e.cur, pub=PUB_FIRST)
        e.seed_615(e.cur, (2023, 2024))
        # pending: a revised CTB year and a revised 615 year, stored but not
        # yet in live, so refresh-latest has something to write
        e.ctb(values={e.codes[0]: {"second_homes": 101}})
        e.ok(e.cur, ["load", "--commit"])
        e.t615((2023, 2024, 2025), latest="26 June 2026", values={
            ("All_long_term_vacants", e.codes615[1], 2023): 115})
        e.ok(e.cur, ["load-615", "--commit"])
        for mode in ([], ["--simulate"]):
            tag = mode[0] if mode else "preview"
            e.ctb(year=2026, pub=PUB_FIRST)
            check(e, f"load {tag}", ["load", "--release", "2026", *mode])
            e.t615((2023, 2024, 2025, 2026), latest="27 June 2026",
                   values={("All_vacants", e.codes615[0], 2023): 333})
            check(e, f"load-615 {tag}", ["load-615", *mode])
            text = check(e, f"refresh-latest {tag}", ["refresh-latest",
                                                      *mode])
            if text.count("(total 0)") == 3:
                problems.append("the refresh preview had nothing to write; "
                                "the check proves little")
            check(e, f"restore-edition {tag}", ["restore-edition", "ctb",
                                                "2025", "1", *mode])
            check(e, f"restore-edition 615 {tag}", ["restore-edition",
                                                    "615", "2023", "1",
                                                    *mode])
            check(e, f"ddl {tag}", ["ddl", *mode])
        check(e, "status", ["status"])

    def legacy_body(e, files):
        for mode in ([], ["--simulate"]):
            before = state(e.cur)
            rc, text, logged = e.migrate_cmd(files, mode)
            if rc != 0 or state(e.cur) != before or logged.called or \
                    e.commits():
                problems.append(f"migrate-legacy {mode or 'preview'}: rc "
                                f"{rc}: {text[-100:]}")
    scenario(cur, body)
    legacy_world(cur, legacy_body)
    report(16, name, not problems,
           "load, load-615, refresh-latest, restore-edition (ctb and 615), "
           "ddl and migrate-legacy, as preview and --simulate, and status, "
           "left live, editions, ledgers and the run log as they were"
           if not problems else "; ".join(problems[:3]))


# --------------------------------------------------------------- gate 17

def gate_17_revision_and_refresh(cur):
    name = ("revision then refresh-latest changes only that period, moves "
            "source_publication on every row and loaded_at")
    problems = []

    def snap(cur, spec, y):
        cur.execute(f"SELECT md5(string_agg(t::text, ',' ORDER BY t::text)) "
                    f"FROM public.{spec.live_table} t WHERE "
                    f"{spec.period_col} = %s", (y,))
        return cur.fetchone()[0]

    def body(e):
        e.ctb(year=2024, pub=PUB_FIRST)
        e.ok(e.cur, ["load", "--commit"])
        e.ctb(year=2025, pub=PUB_FIRST)
        e.ok(e.cur, ["load", "--commit"])
        for s in (Z_CTB, Z_CLS):
            e.cur.execute(f"UPDATE public.{s.live_table} SET loaded_at = "
                          "'2026-01-01'")
        e.seed_615(e.cur, (2023, 2024, 2025))
        e.cur.execute(f"UPDATE public.{Z_615.live_table} SET loaded_at = "
                      "'2026-01-01'")
        before = {(s.name, y): snap(e.cur, s, y) for s in (Z_CTB, Z_CLS)
                  for y in (2024, 2025)}
        before.update({(Z_615.name, y): snap(e.cur, Z_615, y)
                       for y in (2023, 2024, 2025)})
        # revise CTB 2025 and 615 2023
        e.ctb(year=2025, pub=PUB_REVISED, values={
            e.codes[0]: {"second_homes": 101, "class K": 9}})
        e.ok(e.cur, ["load", "--commit"])
        e.t615((2023, 2024, 2025), latest="26 June 2026", values={
            ("All_long_term_vacants", e.codes615[1], 2023): 115})
        e.ok(e.cur, ["load-615", "--commit"])
        if snap(e.cur, Z_CTB, 2025) != before[(Z_CTB.name, 2025)]:
            problems.append("live changed before refresh-latest")
        e.ok(e.cur, ["refresh-latest", "--commit"])
        now = {(s.name, y): snap(e.cur, s, y) for s in (Z_CTB, Z_CLS)
               for y in (2024, 2025)}
        now.update({(Z_615.name, y): snap(e.cur, Z_615, y)
                    for y in (2023, 2024, 2025)})
        changed = sorted(k for k in now if now[k] != before[k])
        if changed != sorted([(Z_CTB.name, 2025), (Z_CLS.name, 2025),
                              (Z_615.name, 2023)]):
            problems.append(f"changed periods {changed}")
        e.cur.execute(f"SELECT COUNT(DISTINCT source_publication), "
                      f"MIN(source_publication) FROM public."
                      f"{Z_CTB.live_table} WHERE taxbase_year = 2025")
        n, src = e.cur.fetchone()
        e.cur.execute(f"SELECT DISTINCT source_file, loaded_at FROM "
                      f"public.{Z_CTB.editions_table} WHERE taxbase_year = "
                      "2025 AND edition = 2")
        (esrc, eload), = e.cur.fetchall()
        e.cur.execute(f"SELECT loaded_at FROM public.{Z_CTB.live_table} "
                      "WHERE taxbase_year = 2025 AND lad24cd = %s",
                      (e.codes[0],))
        moved = e.cur.fetchone()[0]
        e.cur.execute(f"SELECT COUNT(*) FROM public.{Z_CTB.live_table} "
                      "WHERE taxbase_year = 2025 AND lad24cd <> %s AND "
                      "loaded_at <> '2026-01-01'", (e.codes[0],))
        stray = e.cur.fetchone()[0]
        if n != 1 or src != esrc or "revised 2026-01-21" not in src or                 moved != eload or stray:
            problems.append(f"2025 source_publication/loaded_at: {n} "
                            f"value(s), changed row loaded_at {moved} vs "
                            f"edition {eload}, {stray} unchanged row(s) "
                            "moved")
        e.cur.execute(f"SELECT COUNT(DISTINCT loaded_at) FROM public."
                      f"{Z_CTB.live_table} WHERE taxbase_year = 2024")
        if e.cur.fetchone()[0] != 1 or snap(e.cur, Z_CTB, 2024) != before[
                (Z_CTB.name, 2024)]:
            problems.append("2024 was touched")
        e.cur.execute(f"SELECT long_term_vacant_dwellings FROM public."
                      f"{Z_615.live_table} WHERE year = 2023 AND "
                      "published_la_code = %s", (e.codes615[1],))
        if e.cur.fetchone()[0] != 115:
            problems.append("615 2023 not refreshed")
        e.cur.execute(f"SELECT COUNT(*) FROM public.{Z_615.live_table} "
                      "WHERE year <> 2023 AND loaded_at <> '2026-01-01'")
        if e.cur.fetchone()[0]:
            problems.append("a 615 year other than 2023 moved loaded_at")
        for part in m.PARTS:
            for prof in m.profiles(part):
                if not pe.status(e.cur, prof)["ok"]:
                    problems.append(f"status not clean for {part}")
        text, _ = e.ok(e.cur, ["refresh-latest"])
        if text.count("would write: none") != 3:
            problems.append("refresh-latest plans something after the "
                            "refresh")
    scenario(cur, body)
    report(17, name, not problems,
           "after revising CTB 2025 and 615 2023, only those periods "
           "changed; 2025's source_publication is uniformly the revised "
           "edition's and its loaded_at is the edition's; 2024 and the "
           "other 615 years are untouched" if not problems
           else "; ".join(problems[:3]))


# --------------------------------------------------------------- gate 18

EDITION1_HASH_SQL = """
SELECT COUNT(*), md5(string_agg(jsonb_build_object({pairs})::text,
       E'\\n' ORDER BY {order}))
FROM public.{table} e WHERE e.edition = 1
"""


def edition1_hash(cur, spec, live_spec=None):
    """(rows, md5) of every period's edition 1, computed as m.live_state
    does for the live table: each row as jsonb of the live columns (the
    as-loaded wrapper removed from source_publication), without loaded_at
    and null_reasons, ordered by key and period."""
    live = live_spec or spec
    cur.execute("SELECT column_name FROM information_schema.columns WHERE "
                "table_schema = 'public' AND table_name = %s ORDER BY "
                "ordinal_position", (live.live_table,))
    cols = [r[0] for r in cur.fetchall()
            if r[0] not in ("loaded_at", "null_reasons")]
    pairs = []
    for c in cols:
        if c == "source_publication":
            pairs.append("'source_publication', regexp_replace("
                         "e.source_file, '^as loaded: (.*); file dated "
                         "[0-9]{4}-[0-9]{2}-[0-9]{2}$', '\\1')")
        else:
            pairs.append(f"'{c}', e.{c}")
    order = ", ".join(tuple(spec.key_cols) + (spec.period_col,))
    cur.execute(EDITION1_HASH_SQL.format(pairs=", ".join(pairs),
                                         order=order,
                                         table=spec.editions_table))
    return cur.fetchone()


HASH_LABELS = (("ctb", 0), ("classes", 1), ("615", 2))


def note_hashes(note):
    """{label: md5} from lines 'before-state hash (ctb): <32 hex>' of the
    decision note, or {}."""
    p = Path(note)
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8")
    return {lab: mt.group(1) for lab in ("ctb", "classes", "615")
            for mt in [re.search(r"before-state hash \(" + lab + r"\)\s*:?\s*"
                                 r"`?([0-9a-f]{32})", text, re.I)] if mt}


def real_edition1_hash(cur, specs=None, note=NOTE, legacy=None):
    specs = specs or REAL
    _need(cur, specs)
    legacy = m.LEGACY_LIVE if legacy is None else legacy
    bad, parts = [], []
    recorded = note_hashes(note)
    if not Path(note).exists():
        bad.append(f"decision note {Path(note).name} not written")
    for lab, i in HASH_LABELS:
        n, h = edition1_hash(cur, specs[i], REAL[i] if specs is REAL
                             else specs[i])
        want_n, want_h = legacy[lab]
        if (n, h) != (want_n, want_h):
            bad.append(f"{lab}: edition 1 rows/hash {n}/{h}, expected "
                       f"{want_n}/{want_h}")
        if Path(note).exists():
            if lab not in recorded:
                bad.append(f"the decision note records no 'before-state "
                           f"hash ({lab}): ...' line")
            elif recorded[lab] != h:
                bad.append(f"{lab}: edition 1 hash {h} differs from the "
                           f"note's {recorded[lab]}")
        parts.append(f"{lab} {n:,} rows {h}")
    return not bad, "; ".join(bad[:3]) if bad else (
        "edition 1 of every period hashes to the before-state hash in the "
        "decision note (" + "; ".join(parts) + ")")


def gate_18_before_state_hash(cur):
    name = ("edition 1 of each period equals the before-state hash recorded "
            "in the decision note")
    out = {}

    def body(e, files):
        live = {k: v for k, v in e.legacy(e.cur).items()}
        pre = {lab: m.live_state(e.cur, ZZ[i])[:2]
               for lab, i in HASH_LABELS}
        rc, text, _ = e.migrate_cmd(files)
        if rc != 0:
            out["error"] = text[-200:]
            return
        out["same"] = all(edition1_hash(e.cur, ZZ[i]) == pre[lab]
                          for lab, i in HASH_LABELS)
        with tempfile.TemporaryDirectory() as tmp:
            note = Path(tmp) / "note.md"
            note.write_text("\n".join(f"before-state hash ({lab}): "
                                      f"{pre[lab][1]}"
                                      for lab, _ in HASH_LABELS),
                            encoding="utf-8")
            out["note_ok"] = real_edition1_hash(e.cur, ZZ, note, pre)
            note.write_text(note.read_text().replace(pre["615"][1],
                                                     "0" * 32),
                            encoding="utf-8")
            out["note_bad"] = real_edition1_hash(e.cur, ZZ, note, pre)
            out["no_note"] = real_edition1_hash(e.cur, ZZ, Path(tmp) / "x",
                                                pre)
        e.cur.execute(f"UPDATE public.{Z_615.live_table} SET "
                      "vacant_dwellings = COALESCE(vacant_dwellings, 0) + 1 "
                      "WHERE year = 2024 AND published_la_code = 'E06000002'")
        out["changed"] = m.live_state(e.cur, Z_615)[:2] != pre["615"]
        rc, text, _ = e.migrate_cmd(files)
        out["twice"] = rc == "halt" and ("runs once" in text
                                         or "not as surveyed" in text)
    try:
        legacy_world(cur, body)
    except (psycopg2.Error, SystemExit, RuntimeError, AssertionError) as ex:
        return report(18, name, False, str(ex).splitlines()[0])
    ok = (out.get("same") and out.get("note_ok", (0,))[0]
          and not out["note_bad"][0] and "differs" in out["note_bad"][1]
          and not out["no_note"][0] and out.get("changed")
          and out.get("twice"))
    mixed(18, name, ok,
          "a seeded migration stores an edition 1 whose three hashes equal "
          "the before-state hashes; a note with a wrong or missing hash "
          "fails; a later change shows; a second migration is refused"
          if ok else f"seeded: {out}", cur, real_edition1_hash)


# --------------------------------------------------------------- gate 19

def real_migration_proof(cur, specs=None, files=None, raw_dir=None):
    """The migration proof re-run read-only from the files on disk (found by
    sha256): the parser on the held files reproduces every cell of edition
    1 of the three tables, 615 2025 all vacants equals CTB empty_total +
    unoccupied_exemptions_total for every authority, and the files
    reconcile."""
    specs = specs or REAL
    _need(cur, specs)
    files = m.LEGACY_FILES if files is None else files
    raw = Path(raw_dir) if raw_dir is not None else m.RAW_DIR
    by_sha = {m.content_sha256(p): p
              for pat in ("*.xlsx", "*.ods") for p in sorted(raw.glob(pat))}
    read = {}
    for part, f in files.items():
        p = by_sha.get(f["sha256"])
        if p is None:
            return False, (f"{part}: no file in {raw} with sha256 "
                           f"{f['sha256'][:16]}")
        file = m.read_ctb(p) if part == "ctb" else m.read_615(p)
        file["source"] = f["url"]
        bad = m.reconcile(file)
        if bad:
            return False, f"{p.name}: reconciliation failed: {bad[0]}"
        read[part] = file
    with contextlib.redirect_stdout(io.StringIO()):
        ctb_built = m._build(cur, "ctb", read["ctb"])
        t6_built = m._build(cur, "615", read["615"])
    ctb, cls, t6 = specs
    bad, cells = [], {"ctb": 0, "classes": 0, "615": 0}
    held_m = [r for y in sorted(ctb_built)
              for r in m.records(cur, ctb, str(y), 1)]
    held_c = [r for y in sorted(ctb_built)
              for r in m.records(cur, cls, str(y), 1)]
    held_t = [r for y in sorted(t6_built)
              for r in m.records(cur, t6, str(y), 1)]
    built_m = [r for y in sorted(ctb_built) for r in ctb_built[y]["main"]]
    built_c = [r for y in sorted(ctb_built) for r in ctb_built[y]["classes"]]
    built_t = [r for y in sorted(t6_built) for r in t6_built[y]]
    for name, held, built, cols, key in (
            ("ctb", held_m, built_m,
             [c for c in m.CTB_DATA if c != "null_reasons"],
             lambda r: (r["lad24cd"], r["taxbase_year"])),
            ("classes", held_c, built_c,
             ["dwellings", "exemption_description"],
             lambda r: (r["lad24cd"], r["taxbase_year"],
                        r["exemption_class"])),
            ("615", held_t, built_t,
             [c for c in m.T615_DATA if c != "null_reasons"],
             lambda r: (r["published_la_code"], r["year"]))):
        cells[name], diffs = m._proof(name, held, built, cols, key)
        bad += [f"{name}: {d}" for d in diffs[:2]]
    cur.execute(f"SELECT DISTINCT {t6.period_col} FROM "
                f"public.{t6.editions_table}")
    if {int(r[0]) for r in cur.fetchall()} != set(t6_built):
        bad.append("the 615 years held differ from the file's years")
    cross = 0
    for y in sorted(ctb_built):
        if y in t6_built:
            problems = m.cross_check_615_ctb(t6_built[y],
                                             ctb_built[y]["main"], y)
            bad += problems[:2]
            cross = len(ctb_built[y]["main"])
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{cells['ctb']:,} CTB, {cells['classes']:,} class and "
        f"{cells['615']:,} Table 615 cells of edition 1 equal the files on "
        f"disk read now (0 differences); 615 all vacants equals CTB "
        f"empty_total + unoccupied exemptions for {cross} of {cross} "
        "authorities")


def gate_19_migration_proof(cur):
    name = ("the migration proof re-run read-only from the files on disk; "
            "seeded: proof passes, a planted held difference stops "
            "migrate-legacy and stores nothing")
    problems = []

    def ok_world(e, files):
        ctb, t615, fx = files
        rc, text, _ = e.migrate_cmd(files)
        if rc != 0 or "0 differences" not in text:
            problems.append(f"proof did not pass: {text[-150:]}")
            return
        raw = ctb.parent.parent / "raw"
        raw.mkdir()
        for p in (ctb, t615):
            (raw / p.name).write_bytes(p.read_bytes())
        ok, detail = real_migration_proof(e.cur, ZZ, files=fx, raw_dir=raw)
        if not ok:
            problems.append(f"re-run from disk: {detail}")
        # a plant: edition 1 differs from the file
        e.cur.execute(f"SELECT COUNT(*) FROM public.{Z_615.editions_table}")
        # the editions are append-only; plant by pointing the proof at a
        # file whose content differs
        wrong = {**fx, "615": {**fx["615"], "sha256": "0" * 64}}
        ok, detail = real_migration_proof(e.cur, ZZ, files=wrong,
                                          raw_dir=raw)
        if ok or "no file" not in detail:
            problems.append("a file with another sha256 was accepted")
        # a file changed on disk is not found by sha256
        t615.write_bytes(t615.read_bytes() + b"\0")
        ok, detail = real_migration_proof(e.cur, ZZ, files=fx,
                                          raw_dir=ctb.parent.parent)
        if ok or "no file" not in detail:
            problems.append("a changed file on disk was accepted")

    def planted(sql, needle, surveyed_first=False):
        def f(e, files):
            surveyed = dict(e.legacy(e.cur)) if surveyed_first else None
            e.cur.execute(sql)
            surveyed = surveyed or dict(e.legacy(e.cur))
            before = state(e.cur)
            rc, text, logged = e.migrate_cmd(files, legacy=surveyed)
            if not (rc == "halt" and needle in text
                    and state(e.cur) == before and not logged.called):
                problems.append(f"planted {needle!r}: rc {rc}: "
                                f"{text[-150:]}")
        return f
    legacy_world(cur, ok_world)
    legacy_world(cur, planted(
        f"UPDATE public.{Z_615.live_table} SET vacant_dwellings = "
        "vacant_dwellings + 1 WHERE published_la_code = 'E06000002' AND "
        "year = 2024", "proof failed"))
    legacy_world(cur, planted(
        f"UPDATE public.{Z_CLS.live_table} SET dwellings = dwellings + 1 "
        "WHERE lad24cd = 'E06000001' AND exemption_class = 'K'",
        "proof failed"))
    legacy_world(cur, planted(
        f"UPDATE public.{Z_CTB.live_table} SET second_homes = 0 WHERE "
        "lad24cd = 'E06000001'", "proof failed"))
    legacy_world(cur, planted(
        f"DELETE FROM public.{Z_615.live_table} WHERE "
        "published_la_code = 'E06000002' AND year = 2024",
        "not as surveyed", surveyed_first=True))
    mixed(19, name, not problems,
          "seeded migration proof passes (0 differences) and re-runs from "
          "the files by sha256; planted differences in each of the three "
          "tables and a dropped row stop migrate-legacy with nothing stored"
          if not problems else "; ".join(problems[:3]), cur,
          real_migration_proof)


# --------------------------------------------------------------- gate 20

W1_JOIN = re.compile(
    r"LEFT JOIN la_council_taxbase_empties ctb\s+ON ctb\.lad24cd = b\.lad24cd"
    r"\s+AND ctb\.taxbase_year = \(SELECT MAX\(taxbase_year\)\s+FROM "
    r"la_council_taxbase_empties\)\s+LEFT JOIN v_la_empty_homes_rates ctbr\s+"
    r"ON ctbr\.lad24cd = b\.lad24cd")


def w1_query(live_table, view):
    """W1's S22 subqueries of sql/w1/05_la_signals.sql (the latest-year join
    to la_council_taxbase_empties and the join to v_la_empty_homes_rates),
    read from the file, with the table and the view replaced."""
    text = (HERE.parent / "sql" / "w1" / "05_la_signals.sql").read_text(
        encoding="utf-8")
    mt = W1_JOIN.search(text)
    if not mt or "ctbr.lte_rate_pct             AS ctb_lte_rate_pct" \
            not in text:
        raise ValueError("the S22 joins were not found in "
                         "sql/w1/05_la_signals.sql")
    join = re.sub(r"\bla_council_taxbase_empties\b", f"public.{live_table}",
                  mt.group(0))
    join = join.replace("v_la_empty_homes_rates", f"public.{view}")
    return ("SELECT b.lad24cd, ctb.total_dwellings, ctb.empty_6_months_plus,"
            " ctb.empty_homes_premium_count, ctb.second_homes, "
            f"ctbr.lte_rate_pct FROM (SELECT DISTINCT lad24cd FROM public."
            f"{live_table}) b {join}")


def w1_matches_tip(cur, specs, view):
    ctb = specs[0]
    cur.execute(w1_query(ctb.live_table, view))
    got = {r[0]: tuple(r[1:]) for r in cur.fetchall()}
    cur.execute(f"SELECT MAX(taxbase_year) FROM public.{ctb.live_table}")
    (latest,) = cur.fetchone()
    if latest is None:
        return False, "no live rows", {}
    tip = _tip(cur, ctb, latest)
    cur.execute(f"""SELECT lad24cd, total_dwellings, empty_6_months_plus,
        empty_homes_premium_count, second_homes,
        ROUND(empty_6_months_plus::NUMERIC / NULLIF(total_dwellings, 0)
              * 100, 2)
        FROM public.{ctb.editions_table} WHERE taxbase_year = %s AND
        edition = %s""", (latest, tip))
    want = {r[0]: tuple(r[1:]) for r in cur.fetchall()}
    diff = sorted(k for k in set(got) | set(want) if got.get(k) != want.get(k))
    return (not diff and bool(want),
            f"{latest} ed{tip}: {len(want)} authorities, {len(diff)} "
            "differ" + (f", e.g. {diff[:2]}" if diff else ""), want)


def real_w1(cur, specs=None, view="v_la_empty_homes_rates"):
    specs = specs or REAL
    _need(cur, specs)
    ok, detail, want = w1_matches_tip(cur, specs, view)
    nulls = sum(1 for v in want.values() if None in v)
    return ok, (f"W1's subqueries on live return the tip's total_dwellings, "
                f"empty_6_months_plus, empty_homes_premium_count, "
                f"second_homes and the view's lte_rate_pct for {detail}"
                f" ({nulls} with a NULL)" if ok else detail)


def make_view(cur, name, live_table):
    cur.execute("SELECT pg_get_viewdef('public.v_la_empty_homes_rates'::"
                "regclass)")
    sql = re.sub(r"\bla_council_taxbase_empties\b", f"public.{live_table}",
                 cur.fetchone()[0])
    cur.execute(f"CREATE VIEW public.{name} AS {sql}")


def gate_20_w1(cur):
    name = ("W1's subqueries on live return the tip's values for the "
            "latest year (the four counts and the view's lte_rate_pct)")
    out = {}

    def body(e):
        e.ctb(year=2024, pub=PUB_FIRST)
        e.ok(e.cur, ["load", "--commit"])
        e.ctb(year=2025, values={e.codes[1]: {"second_homes": "[x]"},
                                e.codes[2]: {"total_dwellings": "[x]"}})
        e.ok(e.cur, ["load", "--commit"])
        make_view(e.cur, "zz_s22_rates", Z_CTB.live_table)
        out["ok"] = real_w1(e.cur, ZZ, view="zz_s22_rates")
        e.cur.execute(f"UPDATE public.{Z_CTB.live_table} SET second_homes ="
                      " second_homes + 1 WHERE lad24cd = 'E06000001' AND "
                      "taxbase_year = 2025")
        out["drift"] = real_w1(e.cur, ZZ, view="zz_s22_rates")
        e.cur.execute(f"UPDATE public.{Z_CTB.live_table} SET second_homes ="
                      " second_homes - 1 WHERE lad24cd = 'E06000001' AND "
                      "taxbase_year = 2025")
        e.cur.execute(f"UPDATE public.{Z_CTB.live_table} SET "
                      "empty_6_months_plus = empty_6_months_plus + 5 WHERE "
                      "lad24cd = 'E06000001' AND taxbase_year = 2025")
        out["rate"] = real_w1(e.cur, ZZ, view="zz_s22_rates")
        e.cur.execute(f"UPDATE public.{Z_CTB.live_table} SET "
                      "empty_6_months_plus = empty_6_months_plus - 5 WHERE "
                      "lad24cd = 'E06000001' AND taxbase_year = 2025")
        e.cur.execute(f"DELETE FROM public.{Z_CTB.live_table} WHERE "
                      "lad24cd = 'E06000002' AND taxbase_year = 2025")
        out["missing"] = real_w1(e.cur, ZZ, view="zz_s22_rates")
    scenario(cur, body)
    seeded = (out["ok"][0] and not out["drift"][0] and not out["rate"][0]
              and not out["missing"][0])
    mixed(20, name, seeded,
          f"seeded latest year {out['ok'][1]}; a drifted count, a drifted "
          "rate input and a missing authority are each caught" if seeded
          else f"seeded: {out}", cur, real_w1)


# --------------------------------------------------------------- 21 - 23

def gate_21_rerun(cur):
    name = "rerun idempotent: a second load changes nothing"
    problems = []

    def body(e):
        e.ctb()
        e.t615((2023, 2024, 2025))
        e.ok(e.cur, ["load", "--commit"])
        e.ok(e.cur, ["load-615", "--commit"])
        snap = state(e.cur)
        for argv in (["load", "--commit"], ["load-615", "--commit"],
                     ["load", "--release", "2025", "--commit"],
                     ["refresh-latest", "--commit"]):
            rc, text, logged = e.cmd(argv)
            if rc != 0 or state(e.cur) != snap or logged.called:
                problems.append(f"{argv[0]}: changed state or logged: "
                                f"{text[-100:]}")
        # a recheck of an unchanged period adds one ledger row only
        rc, text, logged = e.cmd(["load", "--recheck", "2025", "--commit"])
        eds = (tl.editions(e.cur, Z_CTB, "2025"),
               tl.editions(e.cur, Z_615, "2024"))
        if rc != 0 or eds != ([(1, None, e.n_auth)],
                              [(1, None, len(e.codes615) - 2 + 0)]) and \
                len(eds[0]) != 1:
            problems.append(f"recheck stored an edition: {eds}")
    scenario(cur, body)
    report(21, name, not problems, "a second load, load-615, a held-release "
           "load and refresh-latest stored nothing and logged nothing; a "
           "recheck adds no edition" if not problems
           else "; ".join(problems[:3]))


def gate_22_stranded(cur):
    name = "stranded period repair (editions held, live rows missing)"
    problems = []

    def body(e):
        e.ctb()
        e.t615((2023, 2024, 2025))
        e.ok(e.cur, ["load", "--commit"])
        e.ok(e.cur, ["load-615", "--commit"])
        e.cur.execute(f"DELETE FROM public.{Z_CTB.live_table}")
        e.cur.execute(f"DELETE FROM public.{Z_CLS.live_table}")
        e.cur.execute(f"DELETE FROM public.{Z_615.live_table} WHERE year = "
                      "2024")
        if pe.status(e.cur, m.profiles("ctb")[0])["live_missing"] != [
                "2025"] or pe.status(e.cur, m.profiles("615")[0])[
                "live_missing"] != ["2024"]:
            problems.append("status does not name the stranded periods")
        rc, text, _ = e.cmd(["load", "--commit"])
        rc6, text6, _ = e.cmd(["load-615", "--commit"])
        n = e.n_auth
        if rc != 0 or rc6 != 0 or "live-missing" not in text or \
                tl.editions(e.cur, Z_CTB, "2025") != [(1, None, n)] or \
                tl.editions(e.cur, Z_CLS, "2025") != [(1, None, 11 * n)] or \
                tl.count(e.cur, Z_CTB.live_table) != n or \
                tl.count(e.cur, Z_CLS.live_table) != 11 * n or \
                len(tl.editions(e.cur, Z_615, "2024")) != 1 or \
                tl.count(e.cur, Z_615.live_table, "WHERE year = 2024") == 0:
            problems.append(f"repair failed: {text[-120:]} {text6[-120:]}")
        if "live-missing" not in [o for _, o, _ in tl.ledger(
                e.cur, tl.L_CTB, 2025)]:
            problems.append("no live-missing ledger row for CTB 2025")
        for part in m.PARTS:
            for prof in m.profiles(part):
                if not pe.status(e.cur, prof)["ok"]:
                    problems.append(f"status not clean for {part}")
    scenario(cur, body)
    report(22, name, not problems, "stranded CTB 2025 (main and classes) and "
           "615 2024 were rebuilt from their edition 1 with no new edition "
           "and a live-missing ledger row" if not problems
           else "; ".join(problems[:3]))


def gate_23_restore(cur):
    name = "restore-edition round trip"
    problems = []

    def body(e):
        e.seed_ctb(e.cur, pub=PUB_FIRST)
        first = tl.live_col(e.cur, Z_CTB, "2025", "second_homes")
        e.ctb(values={e.codes[0]: {"second_homes": 101}})
        e.ok(e.cur, ["load", "--commit"])
        e.ok(e.cur, ["refresh-latest", "--part", "ctb", "--commit"])
        snap = state(e.cur)
        rc, text, _ = e.cmd(["restore-edition", "ctb", "2025", "1"])
        if rc != 0 or state(e.cur) != snap:
            problems.append("restore-edition preview wrote")
        rc, text, logged = e.cmd(["restore-edition", "ctb", "2025", "1",
                                  "--commit"])
        n = e.n_auth
        if rc != 0 or tl.editions(e.cur, Z_CTB, "2025")[-1] != (3, 2, n) \
                or tl.editions(e.cur, Z_CLS, "2025")[-1] != (3, 2, 11 * n) \
                or not logged.called:
            problems.append(f"ctb restore --commit: {text[-120:]}")
        e.cur.execute(f"SELECT DISTINCT release_label FROM public."
                      f"{Z_CTB.editions_table} WHERE edition = 3")
        if e.cur.fetchall() != [("restored from edition 1",)]:
            problems.append("restored edition not labelled")
        e.ok(e.cur, ["refresh-latest", "--part", "ctb", "--commit"])
        if tl.live_col(e.cur, Z_CTB, "2025", "second_homes") != first or \
                core.rows_differing(e.cur, Z_CTB, "2025", 3):
            problems.append("refresh-latest did not apply the restored "
                            "CTB edition")
        ed1 = {tuple(sorted(r.items())) for r in m.records(
            e.cur, Z_CLS, "2025", 1)}
        ed3 = {tuple(sorted(r.items())) for r in m.records(
            e.cur, Z_CLS, "2025", 3)}
        if ed1 != ed3:
            problems.append("classes edition 3 does not hold edition 1's "
                            "rows")
        # 615
        e.seed_615(e.cur, (2023, 2024))
        e.t615((2023, 2024), latest="26 June 2026", values={
            ("All_long_term_vacants", e.codes615[1], 2023): 115})
        e.ok(e.cur, ["load-615", "--commit"])
        e.ok(e.cur, ["refresh-latest", "--part", "615", "--commit"])
        rc, text, _ = e.cmd(["restore-edition", "615", "2023", "1",
                             "--commit"])
        e.ok(e.cur, ["refresh-latest", "--part", "615", "--commit"])
        e.cur.execute(f"SELECT long_term_vacant_dwellings FROM public."
                      f"{Z_615.live_table} WHERE year = 2023 AND "
                      "published_la_code = %s", (e.codes615[1],))
        if rc != 0 or e.cur.fetchone()[0] == 115:
            problems.append("615 restore did not apply")
        for argv in (["restore-edition", "ctb", "2025", "3", "--commit"],
                     ["restore-edition", "ctb", "2025", "9", "--commit"],
                     ["restore-edition", "615", "2023", "9", "--commit"]):
            before = state(e.cur)
            ok, _ = e.halts(argv, before)
            if not ok:
                problems.append(f"{' '.join(argv[:4])} did not halt")
        for part in m.PARTS:
            for prof in m.profiles(part):
                if not pe.status(e.cur, prof)["ok"]:
                    problems.append(f"status not clean for {part}")
    scenario(cur, body)
    report(23, name, not problems, "edition 1 stored as edition 3 "
           "('restored from edition 1') for CTB (main and classes together) "
           "and 615, applied by refresh-latest; the tip and a missing "
           "edition are refused" if not problems
           else "; ".join(problems[:3]))


# --------------------------------------------------------------- gate 24

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


def gate_24_no_network_no_secrets(cur):
    name = ("no network, no secret in source or output, nothing written "
            "outside the rolled-back transaction; a download never "
            "overwrites a same-named file with different content")
    files = [Path(__file__).resolve(), HERE / "s22_ctb_editions.py",
             HERE / "period_editions.py", HERE / "geography.py"]
    attempts = []

    def refuse(*a, **k):
        attempts.append(a[:1])
        raise OSError("network blocked by the verify script")
    cur.execute("SELECT COUNT(*) FROM public.pipeline_run_log")
    runs = cur.fetchone()[0]
    cur.execute("SELECT " + ", ".join(
        f"to_regclass('public.{s.editions_table}')" for s in REAL))
    real_before = cur.fetchone()

    def body(c):
        with env(c) as e:
            with mock.patch.object(socket.socket, "connect", refuse), \
                    mock.patch.object(socket, "create_connection", refuse), \
                    mock.patch.object(socket, "getaddrinfo", refuse):
                e.ctb()
                e.t615()
                rc, _, _ = e.cmd(["load", "--commit"])
                rc6, _, _ = e.cmd(["load-615", "--commit"])
                rc_s, _, _ = e.cmd(["status"])
                rc_r, _, _ = e.cmd(["refresh-latest"])
                raw = e.root / "raw"
                body_a = write_ctb(e.root / "dl" / "a.xlsx").read_bytes()
                body_b = write_ctb(e.root / "dl" / "b.xlsx", year=2026,
                                   values={CODES[0]: {"second_homes": 9}}
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
            return rc, rc6, rc_s, rc_r, kept
    try:
        rc, rc6, rc_s, rc_r, kept = _in_savepoint(cur, body)
    except Exception as ex:  # a blocked socket or any other failure
        return report(24, name, False, f"{type(ex).__name__}: {ex}")
    cur.execute("SELECT COUNT(*) FROM public.pipeline_run_log")
    runs_after = cur.fetchone()[0]
    cur.execute("SELECT " + ", ".join(
        f"to_regclass('public.{s.editions_table}')" for s in REAL))
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
    ok = (not attempts and rc == rc6 == rc_s == rc_r == 0 and kept
          and runs == runs_after and real_before == real_after
          and not literal and not in_src and not in_out)
    report(24, name, ok, f"socket attempts {len(attempts)}; load, load-615, "
           f"status and refresh-latest through stubs returned "
           f"{rc}/{rc6}/{rc_s}/{rc_r}; a same-named different download was "
           f"saved beside it, the first untouched={kept}; run-log rows "
           f"{runs}->{runs_after}; real editions tables unchanged="
           f"{real_before == real_after}; secret-like literal in source "
           f"{literal or 'none'}; {len(secrets)} secret-named settings "
           f"checked against {len(files)} files and {len(OUTPUT)} output "
           f"lines: in source {in_src or 'none'}, in output "
           f"{in_out or 'none'}")


# --------------------------------------------------------------------- main

GATES = (gate_1_table_shape_and_immutability, gate_2_edition1_and_latest,
         gate_3_pair_tips, gate_4_source_uniform, gate_5_codes,
         gate_6_row_counts, gate_7_null_vs_zero, gate_8_derived,
         gate_9_identity, gate_10_reconciliation, gate_11_cross,
         gate_12_older_file, gate_13_stop_conditions,
         gate_14_one_transaction, gate_15_ledger, gate_16_preview,
         gate_17_revision_and_refresh, gate_18_before_state_hash,
         gate_19_migration_proof, gate_20_w1, gate_21_rerun,
         gate_22_stranded, gate_23_restore, gate_24_no_network_no_secrets)


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
                   f"gate raised {type(e).__name__}: "
                   f"{str(e).splitlines()[0] if str(e) else ''}")


def main():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            setup_throwaway(cur)
            with tl.specs():
                for gate in GATES:
                    run_gate(cur, gate)
    finally:
        conn.rollback()
        conn.close()
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
