"""Gates for the S3b (Census 2021 TS054 tenure, NOMIS NM_2072_1,
la_tenure_2021) editions tables.

Mirrors scripts/s23_rsh_stock_editions_verify.py and
s5_imd_editions_verify.py. Prints `GATE n name: PASS|FAIL`; exits 1 on any
FAIL. It writes nothing, not even a run-log row. The real-table gates read
la_tenure_2021, la_tenure_2021_editions and
la_tenure_2021_editions_file_checks. Until the editions table exists (ddl and
migrate-legacy not yet run) they print `FAIL (pending migration)` with the
words "not yet migrated", so the script exits non-zero until then, like the
other verifiers; nothing is created to make them pass.

The seeded gates never need the real editions table: they run on the
throwaway tables zz_s3b_live / zz_s3b_editions / zz_s3b_editions_file_checks
(a copy of the loader's spec, the generated totals included), created inside
the transaction, and call the loader's own writer and commands
(s3b_tenure_editions.main with the SPEC pointed at the copy, the NOMIS
replies and the downloads stubbed; apply_year; migrate_legacy;
restore_edition) rather than a re-implementation. The real tables are only
ever read.

Gates:
  1  editions table, ledger, append-only triggers (UPDATE, DELETE, TRUNCATE
     refused), shape                                       (throwaway + real)
  2  every live census has edition 1; the latest edition equals live cell
     for cell (key, census_year, the nine counts)                     (real)
  3  live holds exactly one census (census_year 2021 only); a second census
     never reaches live through load, refresh-latest or --accept-drift; it
     is listed as held, not live
  4  identity from the source: the dataset definition (TS054 - Tenure,
     c2021ts054), the geography type on every row, RECORD_COUNT, the
     authority count, the codelist (aggregate codes refused); seeded
     mismatches halt
  5  older-source guard on the API and --file paths; the rank is the
     definition's LastUpdated, never a file name
  6  stop conditions: each seeded REJECTED (a reissue, equal LastUpdated with
     other content, a changed key set, another census into an empty live),
     stores nothing, no ledger row, partial run-log row
  7  ledger rule: the skip needs the (where, content sha256) pair for the
     census and the same LastUpdated; a failing ledger, live or edition
     insert rolls the load back
  8  preview and simulate write nothing
  9  a byte-identical re-read is `unchanged` on every path against edition 1
     'as loaded'; the held reply read now equals edition 1  (seeded + real)
 10  a reissue stores edition 2; refresh-latest changes only the live census,
     copies loaded_at and the label on the changed rows (source is
     provenance, not compared)
 11  edition 1 equals the decision note's before-state line     (seeded + real)
 12  the migration proof re-run read-only from the files on disk (seeded +
     real)
 13  rerun idempotent
 14  restore-edition round trip
 15  no network, no secret in source or output, nothing left committed, a
     download never overwrites a same-named file with different content
 16  the parts sum to the publisher's total on every authority (live, tip,
     edition 1), the generated totals agree and are never written, and a
     file whose total is not the parts' sum halts the load and the
     migration (the total is never built from the parts)  (seeded + real)
 17  no `|| 0` style coercion in the module (the checker's patterns) and no
     assignment to the total (AST)

The decision note's hash lines are scan-safe: a 64-hex run would be flagged by
the credential scan, so each sha256 is written as two 32-hex halves in two
labelled fields:
    before-state la_tenure_2021 2021 rows=<n> sha256-first32=<32 hex> sha256-last32=<32 hex>
`python scripts/s3b_tenure_editions_verify.py --print-note-lines` prints it
for the live table as it stands (read-only; also a before-state-all line over
every stored column), for the decision note. Take it before the migration.
S3b has no W1 or map reader, so there is no w1-read line.

Usage:
    python scripts/s3b_tenure_editions_verify.py
    python scripts/s3b_tenure_editions_verify.py --print-note-lines

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
from pathlib import Path
from unittest import mock

import psycopg2

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))
from _db import ENV, get_conn, get_readonly_conn  # noqa: E402
import check_loaders  # noqa: E402
import editions_core as core  # noqa: E402
import period_editions as pe  # noqa: E402
import s3b_tenure_editions as m  # noqa: E402
import test_s3b_tenure_loader as tl  # noqa: E402
import test_s3b_tenure_pure as tp  # noqa: E402
from test_s3b_tenure_pure import (CODES, DS_2031, LAST, counts,  # noqa: E402
                                  write_csv, write_def)

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
P31 = tl.P31
N = tl.N
LOADER = HERE / "s3b_tenure_editions.py"
NOTE = (HERE.parent / "docs" / "decisions"
        / "2026-10-10-reference-trio-editions-first-load.md")
NOT_YET = ("the S3b editions table does not exist yet (not yet migrated)")
RESULTS = []
OUTPUT = []  # every line the gates print, for the secret-leak gate (15)
TABLE = "la_tenure_2021"
LATER = "2024-06-01 09:30:00"
EARLIER = "2022-11-02 09:30:00"
FUTURE = "2033-03-01 09:30:00"
SOCIAL = ("social_rented_council", "social_rented_other")
PRIVATE = ("private_rented_landlord", "private_rented_other")


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


specs = tl.specs


def plant(cur, sql, check, args=None):
    """check() after one statement, in a savepoint that is rolled back."""
    cur.execute("SAVEPOINT plant")
    try:
        cur.execute(sql, args)
        return check()
    finally:
        cur.execute("ROLLBACK TO SAVEPOINT plant")
        cur.execute("RELEASE SAVEPOINT plant")


# ------------------------------------------------------------ throwaway data

def setup_throwaway(cur):
    names = (ZZ.live_table, ZZ.editions_table, LEDGER)
    cur.execute("SELECT " + ", ".join(f"to_regclass('public.{n}')"
                                      for n in names))
    if any(cur.fetchone()):
        sys.exit("HARD STOP: a zz_s3b table already exists as a real table; "
                 "refusing to run")
    cur.execute(f"CREATE TABLE public.{ZZ.live_table} (LIKE "
                f"public.{REAL.live_table} INCLUDING ALL)")
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


def bump(code="E06000002", by=1):
    """Overrides that change one authority's count and keep the parts
    summing to the total."""
    c = counts(code)
    return {(code, "owned_outright"): c["owned_outright"] + by,
            (code, "total_households"): c["total_households"] + by}


# ------------------------------------------------------------ files and Env

class Env(tl.Migrate):
    """The loader's main(argv) on the throwaway tables, the NOMIS replies and
    the downloads stubbed; every file is written here (write_csv,
    write_def)."""

    def __init__(self, cur, root):
        super().__init__()
        self.cur = cur
        self.root = Path(root)
        self.api = {m.search_url(): tp.search_json(),
                    m.codelist_url(m.DATASETS[P]): tp.codelist_json()}
        self.urls, self.final, self.fetched = {}, {}, []
        self.n = 0
        self.tx = None

    def run_main(self, cur, argv):
        """main(argv) on the throwaway tables: (rc or 'halt', text, run-log
        mock)."""
        borrowed = tl._Borrowed(cur)
        self.tx = borrowed.tx

        def fetch_api(url, dest, session=None):
            self.fetched.append((url, Path(dest).name))
            return self.urls[url], self.final.get(url, url)
        out = io.StringIO()
        with specs(), \
                mock.patch.object(m, "_conn", return_value=borrowed), \
                mock.patch.object(m, "fetch_json",
                                  side_effect=lambda u, s=None: self.api[u]), \
                mock.patch.object(m, "fetch_api", side_effect=fetch_api), \
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

    def reissue(self, **kw):
        """The held census again, a later LastUpdated, one count changed."""
        kw.setdefault("overrides", bump())
        return self.release(last=LATER, **kw)

    def second_census(self):
        """(csv, def) of a 2031 census, read with --file/--def."""
        return self.release(ds=DS_2031, last=FUTURE, register=False)


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
    """A seeded legacy state (as the old build left it: two source variants
    and two loaded_at values) and the fixture files; fn(env, csv, def,
    files)."""
    def body(c):
        with env(c) as e, specs():
            csvp, defp, files = e.seed_legacy(c, **kw)
            return fn(e, csvp, defp, files)
    return _in_savepoint(cur, body)


@contextmanager
def surveyed(e, files):
    """The loader's surveyed live state pointed at the seeded legacy rows."""
    with mock.patch.object(m, "LEGACY_LIVE", e.legacy(e.cur)), \
            mock.patch.object(m, "LEGACY_LOADED", e.loaded(e.cur)), \
            mock.patch.object(m, "LEGACY_FILE", files):
        yield


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
    fks = len(_constraint_defs(cur, spec.editions_table, "f"))
    if fks != (1 if spec.fk_la_boundaries else 0):
        bad.append(f"{spec.editions_table}: {fks} foreign key(s)")
    checks = " ".join(_constraint_defs(cur, spec.editions_table, "c"))
    if "census_year" not in checks:
        bad.append(f"{spec.editions_table}: the census_year CHECK is "
                   "missing")
    bad += _triggers(cur, spec.editions_table, spec.trigger,
                     spec.truncate_trigger)
    for c in (m.KEY,) + m.VALUES + m.GENERATED + (m.PERIOD, "source",
                                                  "loaded_at"):
        if c not in lcols:
            bad.append(f"{spec.live_table}: {c} missing")
    lc = _columns(cur, led)
    ptype = _family(dict(spec.key_types)[spec.period_col])
    for c, ty, nullable in (("id", "bigint", "NO"), (spec.period_col, ptype,
                                                     "NO"),
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


def gate_1_table_shape_and_immutability(cur):
    name = ("editions table, ledger, live shape (UPDATE, DELETE and TRUNCATE "
            "are refused on the editions and the ledger)")
    bad = shape_problems(cur, ZZ)

    def seed(cur):
        with env(cur) as e, specs():
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


EMPTY = "no live census to check (an empty state is not a pass)"


def _live_periods(cur, spec):
    cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                f"public.{spec.live_table} ORDER BY 1")
    return [str(r[0]) for r in cur.fetchall()]


def _tip(cur, spec, p):
    return core.chain_tip(cur, spec, str(p))


def _both_ways(cur, a, aargs, b, bargs):
    counts_ = []
    for x, xa, y, ya in ((a, aargs, b, bargs), (b, bargs, a, aargs)):
        cur.execute(f"SELECT COUNT(*) FROM (({x}) EXCEPT ALL ({y})) q",
                    xa + ya)
        counts_.append(cur.fetchone()[0])
    return counts_


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
    cols = ", ".join((m.KEY, spec.period_col) + m.VALUES)
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
        f"{len(periods)} live census(es) each have edition 1 and the latest "
        "edition equals live on the key, census_year and the nine counts, "
        "cell for cell")


def gate_2_edition1_and_latest(cur):
    name = ("every live census has edition 1; the latest edition equals "
            "live cell for cell")
    out = {}

    def body(e):
        e.seed(e.cur)
        out["base"] = real_edition1_and_latest(e.cur, ZZ)
        miss = []
        live = ZZ.live_table
        for sql, needle in (
            (f"UPDATE public.{live} SET owned_outright = owned_outright + 1"
             ", total_households = total_households + 1 WHERE lad24cd = "
             "'E06000001'", "edition-only"),
            (f"UPDATE public.{live} SET lives_rent_free = lives_rent_free + "
             "1 WHERE lad24cd = 'E06000001'", "edition-only"),
            (f"DELETE FROM public.{live} WHERE lad24cd = 'E06000001'",
             "edition-only"),
            (f"UPDATE public.{live} SET census_year = 2031", "no edition 1"),
        ):
            ok, detail = plant(e.cur, sql, lambda: real_edition1_and_latest(
                e.cur, ZZ))
            if ok or needle not in detail:
                miss.append(f"{needle!r}: {detail[:80]}")
        out["miss"] = miss
    scenario(cur, body)
    ok = out["base"][0] and not out["miss"]
    mixed(2, name, ok, "seeded complete state passes; four planted drifts "
          "(a count, an unsummed count, a missing row, a census without "
          "edition 1) are each caught" if ok else
          f"{out['base']} {out['miss']}", cur, real_edition1_and_latest)


# ---------------------------------------------------------------- gate 3

def real_single_vintage(cur, spec=REAL, n=AREAS):
    """Live holds census_year 2021 only (n rows); any other census is in the
    editions only and status lists it as held, not live (information, not a
    fault)."""
    _need(cur, spec)
    cur.execute(f"SELECT {spec.period_col}, COUNT(*) FROM "
                f"public.{spec.live_table} GROUP BY 1")
    live = {str(a): b for a, b in cur.fetchall()}
    bad = []
    if set(live) != {m.LIVE_VINTAGE}:
        bad.append(f"live holds census(es) {sorted(live)}, expected only "
                   f"{m.LIVE_VINTAGE}")
    elif live[m.LIVE_VINTAGE] != n:
        bad.append(f"live {m.LIVE_VINTAGE} has {live[m.LIVE_VINTAGE]} rows, "
                   f"expected {n}")
    cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                f"public.{spec.editions_table} ORDER BY 1")
    stored = [str(r[0]) for r in cur.fetchall()]
    held = [p for p in stored if p != m.LIVE_VINTAGE]
    try:
        st = pe.status(cur, m.profile())
    except SystemExit as e:
        bad.append(f"status halts: {_first(e)}"[:120])
    else:
        if sorted(st.get("held_not_live", [])) != held:
            bad.append(f"status lists held-not-live "
                       f"{st.get('held_not_live')}, the editions hold {held}")
    if bad:
        return False, "; ".join(bad[:3])
    return True, (f"live holds census {m.LIVE_VINTAGE} only ({n} rows); "
                  f"held, not live: {held or 'none'}")


def gate_3_single_vintage(cur):
    name = ("live holds exactly one census (2021); a second census never "
            "reaches live; others are held, not live")
    out = {}
    problems = []

    def body(e):
        e.seed(e.cur)
        out["base"] = real_single_vintage(e.cur, ZZ, n=N)
        live = ZZ.live_table
        before = tl.live_md5(e.cur)
        csvp, defp = e.second_census()
        rc, text, _ = e.cmd(["load", "--file", csvp, "--def", defp,
                             "--commit"])
        if rc != 0 or f"{P31}: held-not-live" not in text \
                or tl.editions(e.cur, P31) != [(1, None, N)] \
                or tl.live_md5(e.cur) != before:
            problems.append(f"a second census did not stop at editions: "
                            f"rc {rc}: {text[-140:]}")
        out["held"] = real_single_vintage(e.cur, ZZ, n=N)
        if not out["held"][0] or P31 not in out["held"][1]:
            problems.append(f"held census not reported: {out['held']}")
        rc, text, _ = e.cmd(["refresh-latest", "--commit"])
        if rc != 0 or tl.live_md5(e.cur) != before:
            problems.append("refresh-latest moved live after a second "
                            "census")
        snap = state(e.cur)
        for flag in (P31, "2011"):
            ok, _ = e.halts(["refresh-latest", "--commit", "--accept-drift",
                             flag], snap)
            if not ok:
                problems.append(f"refresh-latest --accept-drift {flag} did "
                                "not halt")
        e.cmd(["load", "--file", csvp, "--def", defp, "--recheck", P31,
               "--commit"])
        if tl.live_md5(e.cur) != before:
            problems.append("a recheck of the second census touched live")
        miss = []
        ok, detail = plant(e.cur, f"UPDATE public.{live} SET census_year = "
                           "2031 WHERE lad24cd = 'E06000001'",
                           lambda: real_single_vintage(e.cur, ZZ, n=N))
        if ok or "live holds census" not in detail:
            miss.append(f"two censuses in live: {detail[:80]}")
        ok, detail = plant(e.cur, f"DELETE FROM public.{live} WHERE "
                           "lad24cd = 'E06000001'",
                           lambda: real_single_vintage(e.cur, ZZ, n=N))
        if ok or "rows, expected" not in detail:
            miss.append(f"a missing row: {detail[:80]}")
        out["miss"] = miss
    scenario(cur, body)
    ok = out["base"][0] and not problems and not out["miss"]
    mixed(3, name, ok, "seeded: census 2031 loaded after 2021 is "
          "stored as editions only (live byte for byte as before), reported "
          "as held, not live, and left alone by load --recheck and "
          "refresh-latest; --accept-drift of it halts; a second census in "
          "live and a missing row are each caught"
          if ok else f"{out} {problems[:3]}", cur, real_single_vintage)


# ---------------------------------------------------------- file gates (4, 5)

def held_files(files=None, raw_dir=None):
    """(csv, definition) on disk: the CSV whose bytes sha256 is the held
    reply's, and a definition whose LastUpdated is the held one's (the
    definition is API output, so it is identified by its own content);
    either may be None."""
    files = m.LEGACY_FILE if files is None else files
    raw = Path(raw_dir) if raw_dir is not None else m.RAW_DIR
    csvp = defp = None
    for p in sorted(raw.glob("*.csv")):
        if m.content_sha256(p) == files["bytes_sha256"]:
            csvp = p
            break
    for p in sorted(raw.glob("*.json")):
        try:
            d = m.dataset_def(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if d["rank"] == m.LEGACY_RANK:
            defp = p
            break
    return csvp, defp


def real_identity(cur=None, files=None, raw_dir=None, spec=REAL, areas=None):
    """The held reply's identity read from its definition and the CSV: the
    dataset (NM_2072_1, TS054 - Tenure, c2021ts054), LastUpdated equal to the
    surveyed one, the geography type on every row, RECORD_COUNT, and the
    authority count; once the editions table exists, every census's tip
    names its definition's LastUpdated."""
    csvp, defp = held_files(files, raw_dir)
    if csvp is None or defp is None:
        return False, (f"no {'CSV' if csvp is None else 'definition'} in "
                       f"{raw_dir or m.RAW_DIR} for the held reply")
    d = m.dataset_def(defp.read_text(encoding="utf-8"))
    with mock.patch.object(m, "EXPECTED_AREAS", areas or AREAS):
        f = m.read_csv(csvp, d["dataset"])
    bad = []
    if (d["id"], d["name"], d["mnemonic"]) != ("NM_2072_1",
                                               "TS054 - Tenure",
                                               "c2021ts054"):
        bad.append(f"{defp.name}: dataset {d['id']} {d['name']!r}")
    if d["census_year"] != m.LIVE_VINTAGE:
        bad.append(f"{defp.name}: census {d['census_year']}")
    if len(f["rows"]) != (areas or AREAS):
        bad.append(f"{csvp.name}: {len(f['rows'])} authorities")
    tips = ""
    if cur is not None and pe.table_exists(cur, spec.editions_table):
        info = m.tip_info(cur, spec)
        off = [v for v, i in info.items() if i["rank"] is None]
        if off:
            bad.append(f"{off[:3]}: the tip's source_file names no NOMIS "
                       "LastUpdated")
        tips = f"; {len(info)} stored census(es), each tip names its file"
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{defp.name}: {d['id']} {d['name']!r} ({d['mnemonic']}), "
        f"{m.rank_text(d['rank'])}; {csvp.name}: {f['record_count']} rows "
        f"(RECORD_COUNT agrees), geography type {f['geography_type']!r} on "
        f"every row, {len(f['rows'])} authorities{tips}")


def gate_4_identity(cur):
    name = ("identity from the source: dataset definition, geography type, "
            "RECORD_COUNT, authority count, codelist; seeded mismatches "
            "halt")
    problems = []

    def body(e):
        e.seed(e.cur)
        before = state(e.cur)

        def stops(label, argv, needle, **kw):
            ok, text = e.halts(argv, before, needle)
            if not ok:
                problems.append(f"{label}: {text[-100:]}")
        csvp, _ = e.release(register=False)
        other = write_def(e.root / "other.json",
                          name="TS055 - Accommodation type",
                          mnemonic="c2021ts055")
        stops("another dataset", ["load", "--file", csvp, "--def", other,
                                  "--commit"], "TS055")
        stops("no --def", ["load", "--file", csvp, "--commit"], "--def")
        d = write_def(e.root / "good.json", last=LATER)
        for label, kw, needle in (
            ("geography type", dict(geography_type=tp.GT_2021), "April 2021"),
            ("authority count", dict(codes=CODES[:-1]), f"{N - 1} authorit"),
            ("RECORD_COUNT", dict(record_count=7), "RECORD_COUNT"),
            ("new codes", dict(codes=["E06000001", "E06000002", "E08000038",
                                      "E08000019", "E09000033"]), "E08000038"),
            ("a part not summing", dict(overrides={
                ("E06000002", "total_households"): 60000}), "E06000002"),
        ):
            bad_csv, _ = e.release(register=False, **kw)
            stops(label, ["load", "--file", bad_csv, "--def", d, "--commit"],
                  needle)
        # an aggregate category code in the codelist halts the API path
        e.release()
        e.api[m.codelist_url(m.DATASETS[P])] = tp.codelist_json([
            (1, "Owned: Owns outright", 1001, "derived"),
            (0, "Total: All households", None, "total")])
        stops("an aggregate code", ["load", "--commit"], "aggregate")
        e.api[m.codelist_url(m.DATASETS[P])] = tp.codelist_json()
        # discovery: two TS054 datasets, or none
        e.api[m.search_url()] = tp.search_json([
            ("NM_2072_1", "TS054 - Tenure", "c2021ts054"),
            ("NM_8888_1", "TS054 - Tenure", "c2031ts054")])
        stops("two TS054 datasets", ["load", "--commit"], "NM_8888_1")
        e.api[m.search_url()] = tp.search_json([])
        stops("no dataset", ["load", "--commit"], "NM_2072_1")
    scenario(cur, body)
    seeded = not problems
    sd = ("seeded: another dataset definition, a missing --def, a "
          "2021-boundary geography type, 295 authorities, a wrong "
          "RECORD_COUNT, a new Barnsley code, parts that do not sum, an "
          "aggregate category code and a second or no TS054 dataset each "
          "halt and store nothing" if seeded else "; ".join(problems[:3]))
    if not seeded:
        return report(4, name, False, sd)
    try:
        ok, detail = real_identity(cur if exists(cur) else None)
    except (psycopg2.Error, SystemExit, ValueError, LookupError) as ex:
        return report(4, name, False, _first(ex))
    report(4, name, ok, f"{sd}; held reply: {detail}")


def real_rank(cur=None, files=None, raw_dir=None, spec=REAL, areas=None):
    """The held reply's rank is its definition's LastUpdated (never a file
    name), and equals the rank edition 1 records."""
    csvp, defp = held_files(files, raw_dir)
    if defp is None:
        return False, f"no definition in {raw_dir or m.RAW_DIR} for the held"\
            " reply"
    d = m.dataset_def(defp.read_text(encoding="utf-8"))
    if d["rank"] != m.LEGACY_RANK:
        return False, f"{defp.name}: rank {d['rank']}"
    detail = f"{defp.name}: rank {m.rank_text(d['rank'])} (LastUpdated)"
    if cur is not None and pe.table_exists(cur, spec.editions_table):
        cur.execute(f"SELECT DISTINCT source_file FROM "
                    f"public.{spec.editions_table} WHERE {spec.period_col} = "
                    "%s AND edition = 1", (int(d["census_year"]),))
        rows = cur.fetchall()
        if len(rows) != 1 or m.release_rank(rows[0][0]) != d["rank"]:
            return False, (f"{defp.name}: edition 1 records rank "
                           f"{m.release_rank(rows[0][0]) if rows else None},"
                           f" the definition {d['rank']}")
        detail += "; edition 1 records the same rank"
    return True, detail


def gate_5_older_file(cur):
    name = ("older-source guard on the API and --file paths; the rank is "
            "the definition's LastUpdated, never a file name")
    problems = []

    def older(e, how, same=False):
        e.seed(e.cur)
        # LastUpdated 2022-11-02 (older); the CSV's NAME looks newer
        csvp, defp = e.release(last=EARLIER, register=how != "file",
                               overrides=None if same else bump())
        argv = ["load", "--commit"]
        if how == "file":
            named = csvp.with_name("NM_2072_1_TYPE424_2099-12-31.csv")
            csvp.rename(named)
            argv += ["--file", named, "--def", defp]
        return argv

    def body(e):
        for how in ("api", "file"):
            def one(c, how=how):
                with env(c) as e2, specs():
                    argv = older(e2, how)
                    before = state(c)
                    rc, text, logged = e2.cmd(argv)
                    if not (rc == "halt" and f"{P} (older" in text
                            and "older" in text and state(c) == before
                            and not logged.called):
                        problems.append(f"{how}: rc {rc}: {text[-160:]}")
            _in_savepoint(e.cur, one)

        def override(c):
            with env(c) as e2, specs():
                argv = older(e2, "file", same=True) + ["--allow-older-file"]
                rc, text, logged = e2.cmd(argv)
                eds = tl.editions(c)
                if not (rc == 0 and f"{P}: unchanged" in text
                        and eds == [(1, None, N)] and logged.called
                        and "--allow-older-file given"
                        in logged.call_args[0][2]):
                    problems.append(f"--allow-older-file: rc {rc}, {eds}")
        _in_savepoint(e.cur, override)

        def name_is_not_evidence(c):
            if m.release_rank("NM_2072_1_TYPE424_2099-12-31.csv") is not None:
                problems.append("a file name gave a rank")
            with env(c) as e2, specs():
                e2.seed(c)
                # same content under a later LastUpdated is unchanged and
                # raises the tip's rank
                e2.release(last=LATER)
                rc, text, _ = e2.cmd(["load", "--commit"])
                if rc != 0 or f"{P}: unchanged" not in text:
                    problems.append(f"a later LastUpdated, same content: rc "
                                    f"{rc}: {text[-100:]}")
                e2.release()
                ok, _ = e2.halts(["load", "--commit"], state(c))
                if not ok:
                    problems.append("the earlier definition was accepted "
                                    "after a later one")
        _in_savepoint(e.cur, name_is_not_evidence)
    scenario(cur, body)
    seeded = not problems
    sd = ("a definition of LastUpdated 2022-11-02 (the CSV named like "
          "2099-12-31), offered by the API and --file against a held "
          "2023-01-05 reply, is skipped and halts with nothing stored or "
          "logged; --allow-older-file overrides and is logged; a file name "
          "gives no rank; a later LastUpdated moves the tip's rank"
          if seeded else "; ".join(problems[:3]))
    mixed(5, name, seeded, sd, cur, real_rank)


# ---------------------------------------------------------------- gate 6-8

def rejects(e, argv, needle, before, *, commit=True, vintage=P):
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
                f"rejected ['{vintage}'" not in notes:
            return f"run-log notes: {notes[:120]}"
    elif logged.called:
        return "a preview wrote a run-log row"
    return None


def gate_6_stop_conditions(cur):
    name = ("stop conditions: each seeded REJECTED, stores nothing, no "
            "ledger row, partial run-log row")
    problems = []

    def case(label, setup, needle, commit=True, vintage=P):
        def body(c):
            with env(c) as e, specs():
                argv = setup(e) or ["load"]
                argv = argv + (["--commit"] if commit else [])
                before = state(c)
                bad = rejects(e, argv, needle, before, commit=commit,
                              vintage=vintage)
                if bad:
                    problems.append(f"{label}: {bad}")
        _in_savepoint(cur, body)

    def equal_rank(e):
        e.seed(e.cur)
        e.release(overrides=bump())

    def later_rank(e):
        e.seed(e.cur)
        e.release(last=LATER, overrides=bump())

    def partial_keys(e):
        e.seed(e.cur)
        e.release(last=LATER, codes=CODES[:-1] + ["E09000001"])

    def empty_live(e):
        csvp, defp = e.second_census()
        return ["load", "--file", csvp, "--def", defp]
    case("equal LastUpdated, other content", equal_rank,
         "two sources claim the same release")
    case("equal LastUpdated, preview exits 1 and logs nothing", equal_rank,
         "two sources claim the same release", commit=False)
    case("a later LastUpdated, other content", later_rank,
         "--accept-reissue 2021")
    case("a reissue with another key set", partial_keys, m.PARTIAL)
    case("another census into an empty live table", empty_live,
         "empty live table", vintage=P31)

    def accepted(c):
        with env(c) as e, specs():
            e.seed(c)
            e.release(last=LATER, overrides=bump())
            rc, text, logged = e.cmd(["load", "--commit", "--accept-reissue",
                                      P])
            if not (rc == 0 and "ACCEPTED REISSUE" in text
                    and tl.editions(c) == [(1, None, N), (2, 1, N)]
                    and "--accept-reissue" in logged.call_args[0][2]):
                problems.append(f"--accept-reissue: rc {rc}: {text[-120:]}")
    _in_savepoint(cur, accepted)

    def wrong_year(c):
        with env(c) as e, specs():
            e.release()
            ok, _ = e.halts(["load", "--commit", "--accept-reissue", "2011"],
                            state(c), "2011")
            if not ok:
                problems.append("--accept-reissue of a census not compared "
                                "did not halt")
    _in_savepoint(cur, wrong_year)

    def sums(c):
        with env(c) as e, specs():
            e.seed(c)
            e.release(last=LATER, overrides={
                ("E06000002", "total_households"): 60000})
            halted, text = e.halts(["load", "--commit"], state(c),
                                   "E06000002")
            if not halted:
                problems.append("parts that do not sum to the total did not "
                                "halt")
    _in_savepoint(cur, sums)
    report(6, name, not problems, "equal LastUpdated with other content, a "
           "later LastUpdated with other content, a reissue changing the key "
           "set and another census into an empty live table are each "
           "REJECTED (exit 1) with nothing stored, no ledger row and one "
           "partial run-log row (none in a preview); --accept-reissue stores "
           "edition 2 and is logged; parts that do not sum halt"
           if not problems else "; ".join(problems[:3]))


def gate_7_ledger_rule(cur):
    name = ("ledger rule: the skip needs the (where, content sha256) pair "
            "for the census and the same LastUpdated; a failing insert "
            "rolls the load back")
    problems = []

    def body(e):
        e.seed(e.cur)
        e.cur.execute(f"SELECT file_sha256, source_file FROM "
                      f"public.{LEDGER}")
        (sha, src), = e.cur.fetchall()
        rc, text, logged = e.cmd(["load", "--commit"])
        if rc != 0 or "nothing compared" not in text or logged.called:
            problems.append(f"a held pair did not skip: rc {rc}: "
                            f"{text[-100:]}")
        # the same content under a later LastUpdated: the ledger row declares
        # another rank, so it is compared (and found unchanged)
        e.release(last=LATER)
        rc, text, _ = e.cmd(["load"])
        if "nothing compared" in text or f"{P}: unchanged" not in text:
            problems.append("the same content under a later LastUpdated "
                            "was skipped")
        # --recheck reads a held pair
        e.release()
        rc, text, _ = e.cmd(["load", "--recheck", P])
        if f"{P}: unchanged" not in text:
            problems.append("--recheck did not compare a ledger-held reply")
    scenario(cur, body)

    def other_year(e):
        # the pair under another census year in the ledger only
        e.release()
        e.cur.execute(f"INSERT INTO public.{LEDGER} (census_year, "
                      "source_file, file_sha256, outcome, edition) VALUES "
                      "(2031, %s, %s, 'unchanged', 1)",
                      (m.ledger_source(m.data_url(m.category_codes(
                          tp.codelist_json())), P, m.LEGACY_RANK),
                       "0" * 64))
        rc, text, _ = e.cmd(["load"])
        if rc != 0 or f"{P}: new" not in text:
            problems.append(f"an unrelated ledger row blocked the load: "
                            f"rc {rc}")
    scenario(cur, other_year)

    def failing(label, target, attr):
        def body(c):
            with env(c) as e, specs():
                e.release()
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
    report(7, name, not problems, "a held pair skips with nothing compared "
           "or logged; the same content under a later LastUpdated is "
           "compared; --recheck reads a held reply; an unrelated ledger row "
           "does not block; a failing ledger, live or edition insert rolls "
           "back all three and logs a partial run" if not problems
           else "; ".join(problems[:3]))


def gate_8_preview(cur):
    name = "preview and simulate write nothing"
    problems = []
    cur.execute("SELECT COUNT(*) FROM public.pipeline_run_log")
    runs = cur.fetchone()[0]

    def body(e):
        e.release()
        before = state(e.cur)
        for argv in (["load"], ["load", "--simulate"]):
            rc, text, logged = e.cmd(argv)
            if rc != 0 or state(e.cur) != before or logged.called:
                problems.append(f"new census {argv}: rc {rc}")
        e.ok(e.cur, ["load", "--commit"])
        e.reissue()
        before = state(e.cur)
        for argv in (["load"], ["load", "--simulate"], ["status"]):
            rc, text, logged = e.cmd(argv)
            if rc not in (0, 1) or state(e.cur) != before or logged.called:
                problems.append(f"{argv}: rc {rc}, state kept="
                                f"{state(e.cur) == before}")
        e.ok(e.cur, ["load", "--commit", "--accept-reissue", P])
        before = state(e.cur)
        for argv in (["refresh-latest"], ["restore-edition", P, "1"],
                     ["refresh-latest", "--simulate"]):
            rc, text, logged = e.cmd(argv)
            if rc != 0 or state(e.cur) != before or logged.called:
                problems.append(f"{argv} after a reissue: rc {rc}")
    scenario(cur, body)

    def legacy(e, csvp, defp, files):
        before = state(e.cur)
        with surveyed(e, files):
            for argv in (["migrate-legacy", "--file", csvp, "--def", defp],
                         ["migrate-legacy", "--file", csvp, "--def", defp,
                          "--simulate"], ["ddl"]):
                rc, text, logged = e.cmd(argv)
                if rc != 0 or state(e.cur) != before or logged.called:
                    problems.append(f"{argv[0]} {argv[-1]}: rc {rc}")
    legacy_world(cur, legacy)
    cur.execute("SELECT COUNT(*) FROM public.pipeline_run_log")
    if cur.fetchone()[0] != runs:
        problems.append("the run log changed")
    report(8, name, not problems, "load, refresh-latest, restore-edition, "
           "migrate-legacy and ddl in preview and --simulate leave the "
           "editions, the live table and the ledger as they were and log "
           "nothing" if not problems else "; ".join(problems[:3]))


# ---------------------------------------------------------------- gate 9

def real_held_reread(cur, spec=REAL, files=None, raw_dir=None, areas=None):
    """The held reply read again has the content sha256 of edition 1 'as
    loaded' (the nine counts), so a re-read by the API or --file/--def is
    unchanged, never a reissue."""
    _need(cur, spec)
    csvp, defp = held_files(files, raw_dir)
    if csvp is None or defp is None:
        return False, (f"no {'CSV' if csvp is None else 'definition'} in "
                       f"{raw_dir or m.RAW_DIR} for the held reply")
    d = m.dataset_def(defp.read_text(encoding="utf-8"))
    with mock.patch.object(m, "EXPECTED_AREAS", areas or AREAS):
        f = m.read_csv(csvp, d["dataset"])
    with contextlib.redirect_stdout(io.StringIO()):
        built = m._build(cur, f)
    period = f["census_year"]
    held = m.records(cur, spec, period, 1)
    cur.execute(f"SELECT DISTINCT release_label FROM "
                f"public.{spec.editions_table} WHERE {spec.period_col} = %s "
                "AND edition = 1", (int(period),))
    label = cur.fetchall()
    bad = []
    if label != [(core.AS_LOADED_LATEST_LABEL,)]:
        bad.append(f"{period}: edition 1 is not the 'as loaded' one")
    a, b = m.rows_content_sha(held), m.rows_content_sha(built)
    if a != b:
        bad.append(f"{period}: edition 1 content sha {a[:12]} differs from "
                   f"the held reply read now {b[:12]}")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{csvp.name} read now has edition 1's content sha256 ({a[:12]}.., "
        f"{len(held):,} rows): every re-read is unchanged")


def gate_9_byte_identical_reread(cur):
    name = ("a byte-identical re-read is unchanged on every path against "
            "edition 1 'as loaded'")
    problems = []
    out = {}

    def body(e, csvp, defp, files):
        legacy, loaded = e.legacy(e.cur), e.loaded(e.cur)
        before_live = tl.live_md5(e.cur)
        plan, text = e.migrate(e.cur, csvp, defp, files, legacy=legacy,
                               loaded=loaded)
        if "0 differences" not in text:
            problems.append(f"the migration proof: {text[-120:]}")
            return
        d = Path(e.root) / "disk"
        d.mkdir()
        (d / csvp.name).write_bytes(csvp.read_bytes())
        (d / defp.name).write_bytes(defp.read_bytes())
        with mock.patch.object(m, "LEGACY_RANK", m.dataset_def(
                defp.read_text(encoding="utf-8"))["rank"]):
            out["real"] = real_held_reread(
                e.cur, ZZ, {"bytes_sha256": m.content_sha256(csvp)}, d,
                areas=N)
        e.urls[m.def_url(m.DATASETS[P])] = defp
        e.urls[files["url"]] = csvp
        snap = tables_state(e.cur)
        rc, text, _ = e.cmd(["load"])
        if "nothing compared" not in text:
            problems.append("the held reply was compared again by the API")
        for argv in (["load", "--recheck", P],
                     ["load", "--file", csvp, "--def", defp],
                     ["load", "--file", csvp, "--def", defp, "--commit"],
                     ["load", "--file", csvp, "--def", defp, "--recheck", P,
                      "--commit"],
                     ["load", "--recheck", P, "--commit"]):
            rc, text, _ = e.cmd(argv)
            if rc != 0 or f"{P}: unchanged" not in text \
                    or "REJECTED" in text or "two sources claim" in text:
                problems.append(f"{' '.join(map(str, argv[:4]))}: rc {rc}: "
                                f"{text[-120:]}")
        if tables_state(e.cur) != snap or tl.editions(e.cur) != [
                (1, None, N)]:
            problems.append("a re-read stored an edition or moved live")
        if tl.live_md5(e.cur) != before_live:
            problems.append("live changed (both source variants and both "
                            "loaded_at values must be kept)")
        if [o for _, _, o, _ in tl.ledger(e.cur)] != ["unchanged"] * 4:
            problems.append("the re-reads did not add 'unchanged' rows only")
        rc, text, _ = e.cmd(["refresh-latest"])
        if "would write: none" not in text:
            problems.append("refresh-latest planned a write after a re-read")
        if not pe.status(e.cur, m.profile())["ok"]:
            problems.append("status not clean")
        other = write_csv(Path(e.root) / "disk2" / csvp.name,
                          overrides=bump())
        d2 = Path(e.root) / "disk2"
        (d2 / defp.name).write_bytes(defp.read_bytes())
        ok, detail = real_held_reread(
            e.cur, ZZ, {"bytes_sha256": m.content_sha256(other)}, d2,
            areas=N)
        if ok or "differs" not in detail:
            problems.append("a reply with other content was called equal")
    legacy_world(cur, body)
    seeded = not problems and out.get("real", (False,))[0]
    sd = ("seeded: after migrate-legacy the same reply read by the API, "
          "--file/--def, --recheck and with --commit is unchanged against "
          "edition 1, stores no edition, adds 'unchanged' ledger rows only "
          "and leaves live (both source variants, both loaded_at values) "
          "and status alone; the held reply read now has edition 1's "
          "content sha" if seeded else f"{out} {problems[:3]}")
    mixed(9, name, seeded, sd, cur, real_held_reread)


# --------------------------------------------------------------- gate 10

def gate_10_reissue_and_refresh(cur):
    name = ("a reissue stores edition 2; refresh-latest changes only the "
            "live census and copies loaded_at and the label on the changed "
            "rows")
    problems = []

    def body(e):
        e.seed(e.cur)
        csvp, defp = e.second_census()
        e.ok(e.cur, ["load", "--file", csvp, "--def", defp, "--commit"])
        e.cur.execute(f"UPDATE public.{ZZ.live_table} SET loaded_at = "
                      "'2026-01-01'")
        e.cur.execute(f"SELECT md5(string_agg(t::text, ',' ORDER BY "
                      f"t::text)) FROM public.{ZZ.editions_table} t WHERE "
                      "census_year = 2031")
        held_before = e.cur.fetchone()[0]
        first = tl.live_val(e.cur, "E06000002")
        e.reissue()
        rc, text, _ = e.cmd(["load", "--commit", "--accept-reissue", P])
        if rc != 0 or tl.editions(e.cur) != [(1, None, N), (2, 1, N)] or \
                tl.editions(e.cur, P31) != [(1, None, N)]:
            problems.append(f"the reissue did not store edition 2 of {P} "
                            f"only: rc {rc}: {text[-120:]}")
        if tl.live_val(e.cur, "E06000002") != first:
            problems.append("the reissue reached live before refresh-latest")
        if pe.status(e.cur, m.profile())["pending_refresh"] != [P]:
            problems.append("status does not show the refresh pending")
        snap = state(e.cur)
        rc, text, logged = e.cmd(["refresh-latest"])
        if rc != 0 or state(e.cur) != snap or logged.called:
            problems.append("the refresh-latest preview wrote")
        rc, text, logged = e.cmd(["refresh-latest", "--commit"])
        if rc != 0:
            problems.append(f"refresh-latest --commit: rc {rc}: "
                            f"{text[-120:]}")
        if tl.live_val(e.cur, "E06000002") != first + 1:
            problems.append("live does not hold the reissued count")
        e.cur.execute(f"SELECT lad24cd FROM public.{ZZ.live_table} WHERE "
                      "loaded_at <> '2026-01-01'")
        moved = [r[0] for r in e.cur.fetchall()]
        if moved != ["E06000002"]:
            problems.append(f"loaded_at moved on {moved}, not only on the "
                            "changed row")
        e.cur.execute(f"SELECT DISTINCT loaded_at FROM public."
                      f"{ZZ.editions_table} WHERE census_year = 2021 AND "
                      "edition = 2")
        ed2 = {r[0] for r in e.cur.fetchall()}
        e.cur.execute(f"SELECT loaded_at FROM public.{ZZ.live_table} WHERE "
                      "lad24cd = 'E06000002'")
        if e.cur.fetchone()[0] not in ed2:
            problems.append("loaded_at was not copied from edition 2")
        e.cur.execute(f"SELECT DISTINCT release_label FROM public."
                      f"{ZZ.editions_table} WHERE census_year = 2021 AND "
                      "edition = 2")
        label = e.cur.fetchone()[0]
        if tl.live_val(e.cur, "E06000002", "source") != label or \
                tl.live_val(e.cur, "E06000001", "source") == label:
            problems.append("source is not the new label on the changed row "
                            "only")
        e.cur.execute(f"SELECT COUNT(*) FROM public.{ZZ.live_table} l JOIN "
                      f"public.{ZZ.editions_table} d ON d.lad24cd = "
                      "l.lad24cd AND d.census_year = l.census_year AND "
                      "d.edition = 1 WHERE l.owned_outright IS DISTINCT "
                      "FROM d.owned_outright")
        if e.cur.fetchone()[0] != 1:
            problems.append("refresh-latest changed more than the revised "
                            "row")
        e.cur.execute(f"SELECT md5(string_agg(t::text, ',' ORDER BY "
                      f"t::text)) FROM public.{ZZ.editions_table} t WHERE "
                      "census_year = 2031")
        if e.cur.fetchone()[0] != held_before:
            problems.append("the held census's editions changed")
        e.cur.execute(f"SELECT DISTINCT census_year FROM public."
                      f"{ZZ.live_table}")
        if e.cur.fetchall() != [(2021,)]:
            problems.append("live holds a census other than 2021")
        if not pe.status(e.cur, m.profile())["ok"] or core.rows_differing(
                e.cur, ZZ, P, 2):
            problems.append("live does not equal the tip after the refresh")
        rc, text, _ = e.cmd(["refresh-latest"])
        if "would write: none" not in text:
            problems.append("a second refresh-latest plans a write")
        e.cur.execute(f"UPDATE public.{ZZ.live_table} SET lives_rent_free = "
                      "lives_rent_free + 1, total_households = "
                      "total_households + 1 WHERE lad24cd = 'E06000001'")
        ok, _ = e.halts(["refresh-latest", "--commit"], state(e.cur))
        if not ok:
            problems.append("a drifted live census was overwritten without "
                            "--accept-drift")
    scenario(cur, body)
    report(10, name, not problems, "a reissue (later LastUpdated, "
           "--accept-reissue) waits in edition 2 for refresh-latest "
           "(preview writes nothing); the commit changes exactly the "
           "revised row, copies the edition's loaded_at and label to it "
           "only, leaves the held 2031 editions and the one-census live "
           "table as they were; a second refresh plans nothing; a drifted "
           "live census is not overwritten without --accept-drift"
           if not problems else "; ".join(problems[:3]))


# --------------------------------------------------------------- gate 11

def hash_fields(h):
    """The note's scan-safe form of a sha256: two 32-hex halves in two
    labelled fields (the credential scan flags a 64-hex run)."""
    return f"sha256-first32={h[:32]} sha256-last32={h[32:]}"


def note_hashes(note):
    """{census: (rows, sha256)} from lines `before-state la_tenure_2021
    <year> rows=<n> sha256-first32=<32 hex> sha256-last32=<32 hex>` of the
    decision note (the two halves joined), or {}. A `before-state-all ...`
    line is a different record and does not match."""
    p = Path(note)
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8")
    return {mt.group(1): (int(mt.group(2)), mt.group(3) + mt.group(4))
            for mt in re.finditer(
                rf"(?m)^before-state {TABLE} ([0-9]{{4}}) rows=([0-9]+) "
                r"sha256-first32=([0-9a-f]{32}) "
                r"sha256-last32=([0-9a-f]{32})\s*$", text)}


def live_all_sha(cur, spec=REAL):
    """sha256 of every stored column of the live table: lad24cd|the nine
    counts|the two generated totals|census_year|source|loaded_at (UTC, NULL
    as ''), sorted by lad24cd, LF-joined."""
    names = (m.KEY,) + m.VALUES + m.GENERATED + (m.PERIOD, "source")
    cur.execute(f"SELECT {', '.join(names)}, loaded_at AT TIME ZONE 'UTC' "
                f"FROM public.{spec.live_table}")
    lines = sorted("|".join("" if v is None else str(v) for v in r)
                   for r in cur.fetchall())
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def note_lines_for(cur, spec=REAL):
    """The decision note's hash lines for the live table as it stands
    (read-only): before-state (rows_content_sha, the loader's) and the
    before-state-all line over every stored column. Print them before the
    migration."""
    recs = m.records(cur, spec, m.LIVE_VINTAGE)
    n = len(recs)
    return [
        f"before-state {TABLE} {m.LIVE_VINTAGE} rows={n} "
        f"{hash_fields(m.rows_content_sha(recs))}",
        f"before-state-all {TABLE} {m.LIVE_VINTAGE} rows={n} "
        f"{hash_fields(live_all_sha(cur, spec))}"]


def real_edition1_hash(cur, spec=REAL, note=NOTE, legacy_rows=None):
    """Edition 1 'as loaded' of the migrated census equals the before-state
    line in the decision note: the row count and rows_content_sha, and the
    row count is the surveyed live count."""
    _need(cur, spec)
    legacy_rows = m.LEGACY_LIVE[0] if legacy_rows is None else legacy_rows
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
    for p in migrated:
        recs = m.records(cur, spec, p, 1)
        n, h = len(recs), m.rows_content_sha(recs)
        if n != legacy_rows:
            bad.append(f"{p}: edition 1 has {n} rows, surveyed "
                       f"{legacy_rows}")
        if Path(note).exists():
            if p not in recorded:
                bad.append(f"the decision note records no 'before-state "
                           f"{TABLE} {p} rows=.. sha256-first32=.. "
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
        "edition 1 of every migrated census hashes to the before-state "
        "line in the decision note (" + "; ".join(parts) + ")")


def gate_11_before_state_hash(cur):
    name = ("edition 1 equals the before-state line recorded in the "
            "decision note")
    out = {}

    def body(e, csvp, defp, files):
        legacy, loaded = e.legacy(e.cur), e.loaded(e.cur)
        lines = note_lines_for(e.cur, ZZ)
        e.migrate(e.cur, csvp, defp, files, legacy=legacy, loaded=loaded)
        recs = m.records(e.cur, ZZ, P, 1)
        h = m.rows_content_sha(recs)
        out["same"] = h == m.rows_content_sha(m.records(e.cur, ZZ, P))
        out["scan_safe"] = not any(re.search(r"[0-9a-f]{64}", x)
                                   for x in lines)
        with tempfile.TemporaryDirectory() as tmp:
            note = Path(tmp) / "note.md"
            line = lines[0]
            note.write_text("intro\n" + "\n".join(lines) + "\n",
                            encoding="utf-8")
            out["ok"] = real_edition1_hash(e.cur, ZZ, note, N)
            note.write_text(note.read_text().replace(h[:32], "0" * 32),
                            encoding="utf-8")
            out["bad_hash"] = real_edition1_hash(e.cur, ZZ, note, N)
            note.write_text(lines[1] + "\n", encoding="utf-8")
            out["no_line"] = real_edition1_hash(e.cur, ZZ, note, N)
            note.write_text(line.replace(f"rows={N}", f"rows={N + 1}"),
                            encoding="utf-8")
            out["bad_rows"] = real_edition1_hash(e.cur, ZZ, note, N)
            out["no_note"] = real_edition1_hash(e.cur, ZZ, Path(tmp) / "x",
                                                N)
            note.write_text(line, encoding="utf-8")
            out["wrong_survey"] = real_edition1_hash(e.cur, ZZ, note, N + 5)
        e.cur.execute(f"UPDATE public.{ZZ.live_table} SET owned_outright = "
                      "owned_outright + 1, total_households = "
                      "total_households + 1 WHERE lad24cd = 'E06000001'")
        out["changed"] = m.live_state(e.cur, ZZ)[:2] != legacy
        with mock.patch.object(m, "LEGACY_LIVE", legacy), \
                mock.patch.object(m, "LEGACY_LOADED", loaded), \
                mock.patch.object(m, "LEGACY_FILE", files):
            rc, text, _ = e.cmd(["migrate-legacy", "--file", csvp, "--def",
                                 defp, "--commit"])
        out["twice"] = rc == "halt" and ("runs once" in text
                                         or "not as surveyed" in text
                                         or "already holds" in text)
    try:
        legacy_world(cur, body)
    except (psycopg2.Error, SystemExit, RuntimeError, AssertionError) as ex:
        return report(11, name, False, _first(ex))
    ok = (out.get("same") and out.get("scan_safe")
          and out.get("ok", (0,))[0]
          and not out["bad_hash"][0] and "differs" in out["bad_hash"][1]
          and not out["no_line"][0] and "records no" in out["no_line"][1]
          and not out["bad_rows"][0] and not out["no_note"][0]
          and "not written" in out["no_note"][1]
          and not out["wrong_survey"][0] and out.get("changed")
          and out.get("twice"))
    mixed(11, name, ok,
          "a seeded migration stores an edition 1 whose hash equals the "
          "live rows' and the printed note line (scan-safe: no 64-hex run); "
          "a wrong hash, a wrong row count, a missing line (a "
          "before-state-all line does not count) and a missing note fail; "
          "a later change shows; a second migration is refused" if ok
          else f"seeded: {out}", cur, real_edition1_hash)


# --------------------------------------------------------------- gate 12

def real_migration_proof(cur, spec=REAL, files=None, raw_dir=None,
                         areas=None):
    """The migration proof re-run read-only from the files on disk (the CSV
    found by its bytes sha256, the definition by its LastUpdated): the
    parser on the held reply reproduces every count of edition 1 'as loaded'
    (296 x 9; 0 differences) and the reply's own checks pass."""
    _need(cur, spec)
    csvp, defp = held_files(files, raw_dir)
    if csvp is None or defp is None:
        return False, (f"no {'CSV' if csvp is None else 'definition'} in "
                       f"{raw_dir or m.RAW_DIR} for the held reply")
    d = m.dataset_def(defp.read_text(encoding="utf-8"))
    with mock.patch.object(m, "EXPECTED_AREAS", areas or AREAS):
        f = m.read_csv(csvp, d["dataset"])
    with contextlib.redirect_stdout(io.StringIO()):
        built = m._build(cur, f)
    period = f["census_year"]
    held = m.records(cur, spec, period, 1)
    if not held:
        return False, f"{period}: no edition 1"
    cells, diffs = m._proof(held, built)
    cur.execute(f"SELECT DISTINCT release_label FROM "
                f"public.{spec.editions_table} WHERE {spec.period_col} = %s "
                "AND edition = 1", (int(period),))
    if cur.fetchall() != [(core.AS_LOADED_LATEST_LABEL,)]:
        return False, f"{period}: edition 1 is not the 'as loaded' one"
    return not diffs, (f"{len(diffs)} differences, e.g. {diffs[:2]}"
                       if diffs else
                       f"{cells:,} counts of edition 1 ({len(held):,} rows) "
                       f"equal {csvp.name} read now (0 differences)")


def gate_12_migration_proof(cur):
    name = ("the migration proof re-run read-only from the files on disk; "
            "seeded: proof passes, a planted held difference stops "
            "migrate-legacy and stores nothing")
    problems = []

    def ok_world(e, csvp, defp, files):
        with surveyed(e, files):
            rc, text, _ = e.cmd(["migrate-legacy", "--file", csvp, "--def",
                                 defp, "--commit"])
        if rc != 0 or "0 differences" not in text:
            problems.append(f"proof did not pass: {text[-150:]}")
            return
        raw = Path(e.root) / "raw"
        raw.mkdir()
        (raw / csvp.name).write_bytes(csvp.read_bytes())
        (raw / defp.name).write_bytes(defp.read_bytes())
        fx = {"bytes_sha256": m.content_sha256(csvp)}
        with mock.patch.object(m, "LEGACY_RANK", m.dataset_def(
                defp.read_text(encoding="utf-8"))["rank"]):
            ok, detail = real_migration_proof(e.cur, ZZ, fx, raw, areas=N)
            if not ok or "0 differences" not in detail:
                problems.append(f"re-run from disk: {detail}")
            ok, detail = real_migration_proof(
                e.cur, ZZ, {"bytes_sha256": "0" * 64}, raw, areas=N)
            if ok or "no CSV" not in detail:
                problems.append("a file with another sha256 was accepted")
            (raw / csvp.name).write_bytes(csvp.read_bytes() + b"\n")
            ok, detail = real_migration_proof(e.cur, ZZ, fx, raw, areas=N)
            if ok or "no CSV" not in detail:
                problems.append("a changed file on disk was accepted")
            other = write_csv(Path(e.root) / "raw2" / csvp.name,
                              overrides=bump())
            (Path(e.root) / "raw2" / defp.name).write_bytes(defp.read_bytes())
            ok, detail = real_migration_proof(
                e.cur, ZZ, {"bytes_sha256": m.content_sha256(other)},
                other.parent, areas=N)
            if ok or "differences" not in detail:
                problems.append("a reply that differs from edition 1 passed")

    def planted(sql, needle, surveyed_first=False):
        def f(e, csvp, defp, files):
            sv = (e.legacy(e.cur), e.loaded(e.cur)) if surveyed_first \
                else None
            e.cur.execute(sql)
            sv = sv or (e.legacy(e.cur), e.loaded(e.cur))
            before = state(e.cur)
            with mock.patch.object(m, "LEGACY_LIVE", sv[0]), \
                    mock.patch.object(m, "LEGACY_LOADED", sv[1]), \
                    mock.patch.object(m, "LEGACY_FILE", files):
                rc, text, logged = e.cmd(["migrate-legacy", "--file", csvp,
                                          "--def", defp, "--commit"])
            if not (rc == "halt" and needle in text
                    and state(e.cur) == before and not logged.called):
                problems.append(f"planted {needle!r}: rc {rc}: "
                                f"{text[-150:]}")
        return f
    live = ZZ.live_table
    legacy_world(cur, ok_world)
    legacy_world(cur, planted(
        f"UPDATE public.{live} SET owned_outright = owned_outright + 1, "
        "total_households = total_households + 1 WHERE lad24cd = "
        "'E06000002'", "proof failed"))
    legacy_world(cur, planted(
        f"UPDATE public.{live} SET lives_rent_free = lives_rent_free + 1, "
        "total_households = total_households + 1 WHERE lad24cd = "
        "'E06000001'", "proof failed"))
    legacy_world(cur, planted(
        f"DELETE FROM public.{live} WHERE lad24cd = 'E06000002'",
        "not as surveyed", surveyed_first=True))
    mixed(12, name, not problems,
          "seeded migration proof passes (0 differences) and re-runs from "
          "the files by sha256 and LastUpdated; a changed or missing file "
          "and a reply that is not edition 1 are caught; planted "
          "differences in two counts, and a dropped row, stop "
          "migrate-legacy with nothing stored" if not problems
          else "; ".join(problems[:3]), cur, real_migration_proof)


# ------------------------------------------------------ gates 13 and 14

def gate_13_rerun(cur):
    name = "rerun idempotent: a second load changes nothing"
    problems = []

    def body(e):
        csvp, defp = e.release()
        e.ok(e.cur, ["load", "--commit"])
        snap, led = tables_state(e.cur), len(tl.ledger(e.cur))
        for argv in (["load", "--commit"],
                     ["load", "--file", csvp, "--def", defp, "--commit"],
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
    report(13, name, not problems, "a second load by the API and by "
           "--file/--def and a refresh-latest stored no edition and changed "
           "no live row (re-reads add 'unchanged' ledger rows only); a "
           "ledger-skipped load logs nothing"
           if not problems else "; ".join(problems[:3]))


def gate_14_restore(cur):
    name = "restore-edition round trip"
    problems = []

    def body(e):
        e.seed(e.cur)
        first = tl.live_val(e.cur, "E06000002")
        e.reissue()
        e.ok(e.cur, ["load", "--commit", "--accept-reissue", P])
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
        if tl.live_val(e.cur, "E06000002") != first + 1:
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
    report(14, name, not problems, "edition 1 stored as edition 3 "
           "('restored from edition 1') and applied by refresh-latest; the "
           "tip and a missing edition are refused; a preview writes nothing"
           if not problems else "; ".join(problems[:3]))


# --------------------------------------------------------------- gate 15

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


def gate_15_no_network_no_secrets(cur):
    name = ("no network, no secret in source or output, nothing written "
            "outside the rolled-back transaction")
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
        with env(c) as e, specs():
            with mock.patch.object(socket.socket, "connect", refuse), \
                    mock.patch.object(socket, "create_connection", refuse), \
                    mock.patch.object(socket, "getaddrinfo", refuse):
                e.release()
                rc, _, _ = e.cmd(["load", "--commit"])
                rc_s, _, _ = e.cmd(["status"])
                rc_r, _, _ = e.cmd(["refresh-latest"])
                rc_l, _, _ = e.cmd(["load"])
            return rc, rc_s, rc_r, rc_l
    try:
        rc, rc_s, rc_r, rc_l = _in_savepoint(cur, body)
    except Exception as ex:  # a blocked socket or any other failure
        return report(15, name, False, f"{type(ex).__name__}: {ex}")
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
    ok = (not attempts and rc == rc_s == rc_r == rc_l == 0
          and runs == runs_after and real_before == real_after
          and not literal and not in_src and not in_out)
    report(15, name, ok, f"socket attempts {len(attempts)}; load, status, "
           f"refresh-latest and a preview through stubs returned "
           f"{rc}/{rc_s}/{rc_r}/{rc_l}; run-log rows "
           f"{runs}->{runs_after}; real editions table and ledger "
           f"unchanged={real_before == real_after}; secret-like literal in "
           f"source {literal or 'none'}; {len(secrets)} secret-named "
           f"settings checked against {len(files)} files and "
           f"{len(OUTPUT)} output lines: in source {in_src or 'none'}, in "
           f"output {in_out or 'none'}")


# --------------------------------------------------------------- gate 16

def _generated_problems(cur, spec):
    cur.execute("SELECT column_name, is_generated FROM "
                "information_schema.columns WHERE table_schema = 'public' "
                "AND table_name = %s AND column_name = ANY(%s)",
                (spec.live_table, list(m.GENERATED)))
    got = dict(cur.fetchall())
    return [f"{spec.live_table}.{c} is not a generated column"
            for c in m.GENERATED if got.get(c) != "ALWAYS"]


def real_parts_sum(cur, spec=REAL, n=AREAS):
    """In live, in each census's tip and in edition 1: no count is negative
    and the eight parts sum to the publisher's total on every authority
    (m.checks); in live the two generated totals are generated columns that
    equal their parts (social = council + other, private = landlord +
    other)."""
    _need(cur, spec)
    periods = _live_periods(cur, spec)
    if not periods:
        return False, EMPTY
    bad = _generated_problems(cur, spec)
    cur.execute(f"SELECT COUNT(*) FROM public.{spec.live_table} WHERE "
                f"social_rented_total <> {' + '.join(SOCIAL)} OR "
                f"private_rented_total <> {' + '.join(PRIVATE)}")
    off = cur.fetchone()[0]
    if off:
        bad.append(f"{spec.live_table}: {off} row(s) whose generated totals "
                   "disagree with their parts")
    checked = 0
    for p in periods:
        tip = _tip(cur, spec, p)
        for what, recs in ((f"{spec.live_table} {p}", m.records(cur, spec, p)),
                           (f"{spec.editions_table} {p} ed1",
                            m.records(cur, spec, p, 1)),
                           (f"{spec.editions_table} {p} ed{tip}",
                            m.records(cur, spec, p, tip))):
            if len(recs) != n:
                bad.append(f"{what}: {len(recs)} rows, expected {n}")
                continue
            try:
                m.checks(recs)
                checked += len(recs)
            except ValueError as e:
                bad.append(f"{what}: {_first(e)}")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{checked:,} rows over live, edition 1 and the tip: the eight "
        "parts sum to the publisher's total on every authority, no count "
        "is negative; the generated totals are generated columns and "
        "agree with their parts")


def gate_16_parts_sum_to_total(cur):
    name = ("the parts sum to the publisher's total on every authority and "
            "the generated totals agree; a total is never built from the "
            "parts")
    out = {}
    miss = []

    def body(e):
        e.seed(e.cur)
        out["base"] = real_parts_sum(e.cur, ZZ, n=N)
        live = ZZ.live_table
        for sql, needle in (
            (f"UPDATE public.{live} SET total_households = total_households"
             " + 1 WHERE lad24cd = 'E06000002'", "sum to"),
            (f"UPDATE public.{live} SET lives_rent_free = lives_rent_free + "
             "1 WHERE lad24cd = 'E06000002'", "sum to"),
            (f"UPDATE public.{live} SET owned_outright = -1 WHERE lad24cd = "
             "'E06000002'", "negative"),
        ):
            ok, detail = plant(e.cur, sql, lambda: real_parts_sum(
                e.cur, ZZ, n=N))
            if ok or needle not in detail:
                miss.append(f"{needle!r}: {detail[:80]}")
        ok, detail = real_parts_sum(e.cur, ZZ, n=N + 1)
        if ok or "rows, expected" not in detail:
            miss.append("a wrong authority count passed")
        # the generated totals cannot be written
        for col in m.GENERATED:
            e.cur.execute("SAVEPOINT g")
            try:
                e.cur.execute(f"UPDATE public.{live} SET {col} = 1")
                miss.append(f"{col} was writable")
            except psycopg2.Error:
                pass
            e.cur.execute("ROLLBACK TO SAVEPOINT g")
            e.cur.execute("RELEASE SAVEPOINT g")
        # a file whose total is not the parts' sum halts the load, a
        # reissue and the migration (the total is never replaced by the sum)
        bad = {("E06000002", "total_households"): 60000}
        before = state(e.cur)
        e.release(last=LATER, overrides=bad)
        ok, text = e.halts(["load", "--commit"], before, "E06000002")
        if not ok:
            miss.append(f"a reissue with a wrong total did not halt: "
                        f"{text[-100:]}")
        csvp, defp = e.release(register=False, overrides=bad)
        ok, text = e.halts(["load", "--file", csvp, "--def", defp,
                            "--commit"], before, "E06000002")
        if not ok:
            miss.append("--file with a wrong total did not halt")
        # the publisher's total is stored as published
        out["stored_total"] = tl.live_val(e.cur, "E06000001",
                                          "total_households") == counts(
            "E06000001")["total_households"]
    scenario(cur, body)

    def migration(e, csvp, defp, files):
        with surveyed(e, files):
            pass
        csv2, _ = e.release(register=False, overrides={
            ("E06000002", "total_households"): 60000})
        before = state(e.cur)
        with surveyed(e, files):
            rc, text, logged = e.cmd(["migrate-legacy", "--file", csv2,
                                      "--def", defp, "--commit"])
        out["migration"] = (rc == "halt" and "E06000002" in text
                            and state(e.cur) == before)
    legacy_world(cur, migration)
    ok = (out["base"][0] and not miss and out.get("stored_total")
          and out.get("migration"))
    mixed(16, name, ok, "the seeded state passes; a total that is not the "
          "parts' sum, an unsummed part and a negative count each fail; the "
          "generated totals are not writable; a reissue, a --file load and "
          "a migration with a wrong total halt and store nothing; the "
          "publisher's total is stored as published"
          if ok else f"seeded: {out} {miss[:3]}", cur, real_parts_sum)


# --------------------------------------------------------------- gate 17

def source_problems(path):
    """Problems in a module's source: a zero coercion by the checker's own
    patterns (check_loaders._zero_coercions: `or 0`, `|| 0`, COALESCE(x, 0),
    `else 0`, `, 0)`), and (AST) an assignment to the total, a dict entry
    for the total, or a sum() of the parts stored as total_households."""
    text = Path(path).read_text(encoding="utf-8")
    bad = [f"line {n}: {label}"
           for n, label in check_loaders._zero_coercions(text)]
    tree = ast.parse(text)

    def is_total(node):
        return (isinstance(node, ast.Name) and node.id == "TOTAL") or (
            isinstance(node, ast.Constant) and node.value == m.TOTAL)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = (node.targets if isinstance(node, ast.Assign)
                       else [node.target])
            for t in targets:
                if isinstance(t, ast.Subscript) and is_total(t.slice):
                    bad.append(f"line {node.lineno}: an assignment to the "
                               "total")
        if isinstance(node, ast.Dict):
            for k in node.keys:
                if k is not None and is_total(k):
                    bad.append(f"line {node.lineno}: a dict entry for the "
                               "total")
    return bad


def gate_17_no_coercion(cur):
    name = ("no `|| 0` style coercion in the module (the checker's "
            "patterns) and the total is never assigned")
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "x.py"
        p.write_text(
            'def f(v):\n    return v or 0\n'
            'def g(v):\n    return v or 0  # not a source value\n'
            'Q = "SELECT COALESCE(x, 0) FROM t"\n'
            'rec[TOTAL] = sum(rec[c] for c in PARTS)\n'
            'rec["total_households"] = 1\n'
            'd = {TOTAL: 1}\n'
            'ok = {"a": 1}\n', encoding="utf-8")
        planted = source_problems(p)
    caught = (len(planted) == 5
              and any("line 2" in b for b in planted)
              and any("line 5" in b for b in planted)
              and any("line 6" in b for b in planted)
              and any("line 7" in b for b in planted)
              and any("line 8" in b for b in planted))
    src = source_problems(LOADER)
    ok = caught and not src
    report(17, name, ok, "the checker's zero-coercion patterns find nothing "
           "in the loader; no assignment to the total; planted `or 0`, "
           "COALESCE(x, 0), and three ways of assigning the total are each "
           "caught, a marked counter is not" if ok
           else f"planted {planted}; loader {src[:3]}")


# --------------------------------------------------------------------- main

GATES = (gate_1_table_shape_and_immutability, gate_2_edition1_and_latest,
         gate_3_single_vintage, gate_4_identity, gate_5_older_file,
         gate_6_stop_conditions, gate_7_ledger_rule, gate_8_preview,
         gate_9_byte_identical_reread, gate_10_reissue_and_refresh,
         gate_11_before_state_hash, gate_12_migration_proof, gate_13_rerun,
         gate_14_restore, gate_15_no_network_no_secrets,
         gate_16_parts_sum_to_total, gate_17_no_coercion)


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
        sys.exit("usage: s3b_tenure_editions_verify.py [--print-note-lines]")
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
    mine = [t for t in left if t.startswith("zz_s3b")]
    if mine:
        print(f"LEFTOVER: committed zz_s3b tables in the database: {mine}")
        RESULTS.append(False)
    else:
        print("no zz_s3b table left in the database after the rollback")
    others = [t for t in left if t not in mine]
    if others:
        print(f"NOTE: other zz% relations exist in the database (not made "
              f"by this run): {others}")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
