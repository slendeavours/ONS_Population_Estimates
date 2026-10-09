"""Gates for the S4 (DfE care leaver accommodation) editions tables.

Mirrors scripts/s11_cqc_editions_verify.py and s9a_drd_editions_verify.py.
Prints `GATE n name: PASS|FAIL`; exits 1 on any FAIL. The real-table gates
read care_leaver_accommodation, care_leaver_accommodation_editions and its
file-check ledger. Until the editions table exists (ddl and migrate-legacy
not yet run) they print `FAIL (pending migration)` with the words "not yet
migrated", so the script exits non-zero until then, like the S1, S8b, S9,
S11, S15, S18 and S19 verifiers; nothing is created to make them pass.

The seeded gates never need the real tables: they run on the throwaway tables
zz_s4_live, zz_s4_editions and zz_s4_editions_file_checks (a copy of the
loader's SPEC named zz_s4), created inside the transaction, and call the
loader's own writer and commands (s4_care_leaver_editions.main with SPEC
pointed at the copy and the content API, the data guidance page and the
downloads stubbed; apply_year; migrate_legacy; restore_edition;
core.refresh_latest) rather than a re-implementation. The real tables are only
ever read.

Gates:
  1  tables, ledger, live null_reasons column and shape; append-only triggers
     (UPDATE, DELETE, TRUNCATE refused on the editions and the ledger); on
     the throwaway copy and on the real tables once they exist      (real)
  2  every live year has edition 1; the latest edition equals live cell for
     cell                                                            (real)
  3  live source uniform per year and equal to the tip's             (real)
  4  codes: each in la_boundaries, utla_lad_mapping or a declared predecessor
     (attribution complete); county councils resolved; no Barnsley or
     Sheffield new code; seeded: predecessor attribution, undeclared and
     new-code halts; no private dict in the module (AST check)    (real)
  5  one row per authority, cohort and year; no all-NULL row; seeded: an
     all-z row is not stored, two codes to one key halt             (real)
  6  NULL versus 0 never conflated: seeded for every built column (c is
     NULL with its reason, a published 0 stays 0, k and blank halt)
                                                              (real data)
  7  built columns NULL when a part is NULL, total_care_leavers likewise;
     z, x and mixed reasons (seeded)                              (real data)
  8  22-25 sums all four ages, not age 25 alone (seeded)         (real data)
  9  identity: schema, categories, ages, time_identifier, level, grid, rows,
     range, release; each seeded mismatch halts before any write
 10  older-release guard on the page, --release (dataset ids), --file and
     default paths, ranked by the file's own latest year, never its name
 11  stop conditions: each seeded REJECTED and stores nothing; the limits
     themselves pass
 12  a new year in one transaction (edition, live, two ledger rows); a
     failing live or ledger write rolls all back
 13  ledger skip by (URL, sha256); recheck; a changed sha is parsed
 14  preview, --simulate and migrate-legacy preview write nothing
 15  revision then refresh-latest changes only that year, moves source on
     every row and loaded_at
 16  key changes only when each year is named
 17  edition 1 of every year equals the before-state hash recorded in the
     decision note (real, after migration); seeded migration keeps it
 18  the correction proof re-run read-only from the files on disk (real);
     seeded: proof passes, a planted held difference or unexplained dropped
     key stops migrate-legacy and stores nothing
 19  W1's care leaver join on live returns the tip's 17-21 values for the
     latest year                                                     (real)
 20  rerun idempotent
 21  stranded year repair
 22  restore-edition round trip
 23  no network, no secret in source or output; download keeps a same-named
     file with different content

Usage:
    python scripts/s4_care_leaver_editions_verify.py

Everything runs in one transaction that ends in a rollback; each seeded step
also runs in a savepoint that is rolled back. The commands get a stand-in
connection whose commit only counts. No network, no real-table writes, no
backend is ever terminated.
"""
import ast
import contextlib
import csv
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
from _db import ENV, get_conn  # noqa: E402
import editions_core as core  # noqa: E402
import geography  # noqa: E402
import load_checks  # noqa: E402
import period_editions as pe  # noqa: E402
import s4_care_leaver_editions as m  # noqa: E402
import test_s4_care_leaver_loader as tl  # noqa: E402
from test_s4_care_leaver_pure import file_rows, labels, write_csv  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SPEC, LIVE, TABLE = m.SPEC, m.LIVE, m.TABLE
LEDGER = pe.file_checks_table(m.PROFILE)
ZZ = tl.ZZ
ZL, ZE, ZLEDGER = ZZ.live_table, ZZ.editions_table, tl.LEDGER
CODES, N = tl.CODES, tl.N
NOTE = (HERE.parent.parent / "docs" / "decisions"
        / "2026-10-09-s4-editions-first-load.md")
NOT_YET = f"{TABLE} does not exist yet (not yet migrated)"
RESULTS = []
OUTPUT = []  # every line the gates print, for the secret-leak gate (23)
Y17, Y22 = "17 to 18 years", "19 to 21 years"


def report(n, name, ok, detail="", pending=False):
    RESULTS.append(ok)
    verdict = "PASS" if ok else ("FAIL (pending migration)" if pending
                                 else "FAIL")
    line = f"GATE {n} {name}: {verdict}" + (f"  [{detail}]" if detail else "")
    OUTPUT.append(line)
    print(line)


def table_exists(cur, table=TABLE):
    return pe.table_exists(cur, table)


def _in_savepoint(cur, fn):
    cur.execute("SAVEPOINT h")
    try:
        return fn(cur)
    finally:
        cur.execute("ROLLBACK TO SAVEPOINT h")
        cur.execute("RELEASE SAVEPOINT h")


def _raises(fn, exc=ValueError):
    try:
        fn()
    except exc as e:
        return True, str(e)
    return False, "no error raised"


# ------------------------------------------------------------ throwaway data

def setup_throwaway(cur):
    cur.execute("SELECT to_regclass('public.zz_s4_live'), "
                "to_regclass('public.zz_s4_editions'), "
                f"to_regclass('public.{ZLEDGER}')")
    if any(cur.fetchone()):
        sys.exit("HARD STOP: a zz_s4 table already exists as a real table; "
                 "refusing to run")
    cur.execute("CREATE TABLE public.zz_s4_live (LIKE "
                "public.care_leaver_accommodation INCLUDING ALL)")
    m.create_all(cur, ZZ)
    pe.create_file_checks(cur, m._profile(ZZ))


def state(cur):
    """Everything a refused or previewed run must leave alone."""
    cur.execute(f"SELECT md5(string_agg(t::text, ',' ORDER BY t::text)) "
                f"FROM public.{ZL} t")
    live = cur.fetchone()[0]
    cur.execute(f"SELECT md5(string_agg(t::text, ',' ORDER BY t::text)) "
                f"FROM public.{ZE} t")
    eds = cur.fetchone()[0]
    return (live, eds, tl.ledger(cur), tl.run_log_count(cur))


def ed_rows(cur, year, edition=None, cohort=None):
    """{(lad24cd, age_group): {column: value}} of an edition (default the
    tip) of the throwaway editions table."""
    ed = edition or core.chain_tip(cur, ZZ, str(year))
    cols = ("lad24cd", "age_group") + m.DATA_COLUMNS
    cur.execute(f"SELECT {', '.join(cols)} FROM public.{ZE} WHERE "
                "reporting_year = %s AND edition = %s"
                + (" AND age_group = %s" if cohort else ""),
                (year, ed) + ((cohort,) if cohort else ()))
    return {(r[0], r[1]): dict(zip(cols, r)) for r in cur.fetchall()}


# ------------------------------------------------------------ files and Env

class Env:
    """The loader's main(argv) on the throwaway tables, the content API, the
    data guidance pages and the downloads stubbed; every CSV is written here
    (test_s4_care_leaver_pure.file_rows) in the three real header schemas."""

    def __init__(self, cur, tmp):
        self.cur = cur
        self.root = Path(tmp)
        self.urls, self.pages, self.paths, self.ids = {}, {}, {}, {}
        self.latest = None
        self.fetched = []
        self.n = 0
        self.fname = "{cohort}.csv"

    files = tl.Migration.files
    seed_legacy = tl.Migration.seed_legacy
    migrate = tl.Migration.migrate

    def release(self, slug, years17=None, years22=None, *, values17=None,
                values22=None, allz17=(), allz22=(), codes17=None,
                codes22=None, register=True):
        """Write a release's files; register its page and downloads."""
        self.n += 1
        d = self.root / f"r{self.n}"
        sets, out = [], {}
        for cohort, years, values, allz, codes in (
                ("17-21", years17, values17, allz17, codes17),
                ("22-25", years22, values22, allz22, codes22)):
            if not years:
                continue
            schema = tl.schema_of(slug, cohort)
            rows = file_rows(schema, cohort, codes or CODES, list(years),
                             values=values, allz=allz)
            ds_id = (f"{cohort[:2]}{slug}{self.n:02d}-0000-0000-0000-"
                     "000000000000")
            path = write_csv(d / self.fname.format(cohort=cohort), rows)
            out[cohort] = path
            if register:
                self.urls[m.csv_url(ds_id)] = path
                self.paths[(slug, cohort)] = path
                self.ids[(slug, cohort)] = ds_id
            title = {("17-21", True): tl.T17_NEW, ("17-21", False): tl.T17_OLD,
                     ("22-25", True): tl.T22_NEW,
                     ("22-25", False): tl.T22_OLD}[
                (cohort, schema == "care_leaver_2025")]
            sets.append(tl.dataset(title, ds_id, min(years), max(years),
                                   len(rows) - 1))
        if register:
            self.pages[slug] = tl.page_json(
                slug, sets, published=f"{slug}-11-20T09:30:00")
        return out

    def run_main(self, argv, *, table=True, latest=None, rng=(1, 100),
                 patches=()):
        """main(argv) on the throwaway tables. Returns (rc or 'halt', text,
        borrowed, run-log mock)."""
        borrowed = tl._Borrowed(self.cur)
        api = {"latestRelease": {"slug": latest or self.latest},
               "nextReleaseDate": {"year": 2026, "month": 11}}

        def fetch_csv(url, dest, session=None):
            self.fetched.append(url)
            return self.urls[url]

        def fetch_page(slug, session=None):
            return tl.html_of(self.pages[slug])

        out = io.StringIO()
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch.object(m, "SPEC", ZZ))
            st.enter_context(mock.patch.object(m, "_conn",
                                               return_value=borrowed))
            st.enter_context(mock.patch.object(m, "fetch_api",
                                               return_value=api))
            st.enter_context(mock.patch.object(m, "fetch_page",
                                               side_effect=fetch_page))
            st.enter_context(mock.patch.object(m, "fetch_csv",
                                               side_effect=fetch_csv))
            st.enter_context(mock.patch.object(m, "AUTHORITIES_RANGE", rng))
            logged = st.enter_context(mock.patch.object(m, "log_run"))
            st.enter_context(mock.patch.object(
                m, "table_exists",
                side_effect=lambda c, t: table and pe.table_exists(c, t)))
            for p in patches:
                st.enter_context(p)
            st.enter_context(contextlib.redirect_stdout(out))
            assert m.SPEC is ZZ, "SPEC was not pointed at the throwaway copy"
            try:
                rc = m.main(list(argv))
            except SystemExit as e:
                text = f"{out.getvalue()}\n{e.code}"
                OUTPUT.extend(text.splitlines())
                return "halt", text, borrowed, logged
            finally:
                borrowed.tx.done()
        OUTPUT.extend(out.getvalue().splitlines())
        return rc, out.getvalue(), borrowed, logged

    def migrate_cmd(self, files, argv=("--commit",)):
        """The migrate-legacy command on the seeded legacy table: the
        surveyed constants pointed at the seeded state and files."""
        n, h = m.live_state(self.cur, ZZ)
        return self.run_main(
            ["migrate-legacy", *map(str, files), *argv],
            patches=(mock.patch.object(m, "LIVE_ROWS", n),
                     mock.patch.object(m, "LIVE_HASH", h),
                     mock.patch.object(m, "LEGACY_FILES", self.fx)))

    def seed(self, slug="2024", years17=range(2020, 2025),
             years22=(2023, 2024), **kw):
        self.release(slug, years17, years22, **kw)
        self.latest = slug
        rc, text, _, _ = self.run_main(["load", "--release", slug,
                                        "--commit"])
        if rc != 0:
            raise RuntimeError(f"seed load of release {slug} failed: "
                               f"{text[-300:]}")
        self.fetched.clear()

    def seed_three(self):
        """2019-2023 from a 2023 release (17-21 only, by --file), then the
        2025 release: 2021-2022 unchanged, 2023 revised (22-25 added),
        2024-2025 new."""
        r = self.release("2023", range(2019, 2024), None, register=False)
        rc, text, _, _ = self.run_main(
            ["load", "--release", "2023", "--file-17-21", str(r["17-21"]),
             "--commit"])
        if rc != 0:
            raise RuntimeError(f"seed 2023 failed: {text[-300:]}")
        self.release("2025", range(2021, 2026), (2023, 2024, 2025))
        self.latest = "2025"
        rc, text, _, _ = self.run_main(["load", "--commit"])
        if rc != 0:
            raise RuntimeError(f"seed 2025 failed: {text[-300:]}")
        self.fetched.clear()


@contextmanager
def env(cur):
    with tempfile.TemporaryDirectory() as tmp:
        yield Env(cur, tmp)


def scenario(cur, fn, seed=True, **seedkw):
    """Seed through the loader (optionally), run fn(env), roll all back."""
    def body(c):
        with env(c) as e:
            if seed:
                e.seed(**seedkw)
            return fn(e)
    return _in_savepoint(cur, body)


def legacy_world(cur, fn):
    """A seeded legacy table (as the old builds left it) and the fixture
    files; fn(env, files)."""
    def body(c):
        with env(c) as e:
            files = e.files()
            e.seed_legacy(c, *files)
            return fn(e, files)
    return _in_savepoint(cur, body)


def edit_csv(path, fn):
    """Rewrite a CSV: fn(rows) -> rows (header first)."""
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    rows = fn(rows)
    with open(path, "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(rows)


def halts(e, argv, before, **kw):
    """(True, text) if the command halted or exited 1 and left everything as
    `before` with no run-log row and no commit."""
    rc, text, b, logged = e.run_main(argv, **kw)
    ok = (rc in ("halt", 1) and state(e.cur) == before and not logged.called
          and b.commits == 0)
    return ok, text


# ---------------------------------------------------------------- gate 1

_DTYPE = {"integer": "integer", "numeric": "numeric", "boolean": "boolean",
          "text": "text", "text[]": "ARRAY"}


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


def shape_problems(cur, spec):
    """Problems with the editions table, the live table and the file-check
    ledger of a spec."""
    bad = []
    t, lv = spec.editions_table, spec.live_table
    led = pe.file_checks_table(m._profile(spec))
    for table in (t, lv, led):
        if not pe.table_exists(cur, table):
            bad.append(f"{table} does not exist")
    if bad:
        return bad
    cols = _columns(cur, t)
    need = {"lad24cd": ("character varying", "NO"),
            "age_group": ("character varying", "NO"),
            "reporting_year": ("integer", "NO"),
            "edition": ("integer", "NO"), "supersedes": ("integer", "YES"),
            "release_label": ("text", None), "published_date": ("date", None),
            "source_file": ("text", None), "source_sha256": ("text", None),
            "loaded_at": ("timestamp with time zone", None)}
    for c, (ty, nullable) in need.items():
        got = cols.get(c)
        if got is None:
            bad.append(f"editions: {c} missing")
        elif got[0] != ty or (nullable and got[1] != nullable):
            bad.append(f"editions: {c} is {got}, expected {ty} {nullable}")
    for c, ty in m.VALUE_TYPES + m.EXTRA_TYPES:
        for what, cs in (("editions", cols), ("live", _columns(cur, lv))):
            got = cs.get(c)
            if got is None:
                bad.append(f"{what}: {c} missing")
            elif got[0] != _DTYPE[ty]:
                bad.append(f"{what}: {c} is {got[0]}, expected {_DTYPE[ty]}")
    if _pk(cur, t) != {"lad24cd", "age_group", "reporting_year", "edition"}:
        bad.append("editions: primary key is not (lad24cd, age_group, "
                   "reporting_year, edition)")
    cur.execute("SELECT contype FROM pg_constraint WHERE conrelid = "
                "%s::regclass", (f"public.{t}",))
    if any(r[0] == "f" for r in cur.fetchall()):
        bad.append("editions: a foreign key is present (counties are not in "
                   "la_boundaries)")
    bad += _triggers(cur, t, spec.trigger, spec.truncate_trigger)
    if _pk(cur, lv) != {"lad24cd", "reporting_year", "age_group"}:
        bad.append("live: primary key is not (lad24cd, reporting_year, "
                   "age_group)")
    if "null_reasons" not in _columns(cur, lv):
        bad.append("live: null_reasons column missing (run ddl)")
    lc = _columns(cur, led)
    for c, ty, nullable in (("id", "bigint", "NO"),
                            ("reporting_year", "integer", "NO"),
                            ("source_file", "text", "NO"),
                            ("file_sha256", "text", "NO"),
                            ("outcome", "text", "NO"),
                            ("edition", "integer", "YES"),
                            ("checked_at", "timestamp with time zone", "NO")):
        got = lc.get(c)
        if got is None or tuple(got) != (ty, nullable):
            bad.append(f"ledger: {c} is {got}, expected {(ty, nullable)}")
    cur.execute("""SELECT pg_get_constraintdef(oid) FROM pg_constraint
                   WHERE conrelid = %s::regclass AND contype = 'c'""",
                (f"public.{led}",))
    chk = " ".join(r[0] for r in cur.fetchall())
    if not all(f"'{o}'" in chk for o in pe.FILE_CHECK_OUTCOMES):
        bad.append(f"ledger: outcome check is {chk!r}")
    bad += [f"ledger {p}" for p in _triggers(
        cur, led, f"{spec.name}_file_checks_immutable",
        f"{spec.name}_file_checks_no_truncate")]
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
    name = ("tables, ledger, live null_reasons and shape (UPDATE, DELETE and "
            "TRUNCATE are refused on the editions and the ledger)")
    bad = shape_problems(cur, ZZ)

    def seed(cur):
        with env(cur) as e:
            e.seed(years17=(2024,), years22=(2024,))
    for what, table, stmts in (
            ("editions", ZE, {"UPDATE": f"UPDATE public.{ZE} SET foyers = 1",
                              "DELETE": f"DELETE FROM public.{ZE}",
                              "TRUNCATE": f"TRUNCATE public.{ZE}"}),
            ("ledger", ZLEDGER, {"UPDATE": f"UPDATE public.{ZLEDGER} SET "
                                 "outcome = 'new'",
                                 "DELETE": f"DELETE FROM public.{ZLEDGER}",
                                 "TRUNCATE": f"TRUNCATE public.{ZLEDGER}"})):
        for k, msg in immutability(cur, stmts, seed).items():
            if not (msg and "append-only" in msg):
                bad.append(f"{what} {k} not refused ({msg})")
    scope = "throwaway copy"
    if table_exists(cur):
        scope += " and real tables"
        bad += [f"real: {p}" for p in shape_problems(cur, SPEC)]
    report(1, name, not bad, "; ".join(bad[:4]) if bad else
           f"checked on the {scope}; UPDATE/DELETE/TRUNCATE raise append-only "
           "on the editions table and the ledger")


# ------------------------------------------------------- real-table gates

def mixed(n, name, seeded_ok, seeded_detail, cur, real_fn):
    """A gate with a seeded part (always) and a real part (once the editions
    table exists, else pending)."""
    if not seeded_ok:
        return report(n, name, False, f"seeded: {seeded_detail}")
    if not table_exists(cur):
        return report(n, name, False, f"seeded part passes; real: {NOT_YET}",
                      pending=True)
    try:
        ok, detail = real_fn(cur)
    except (psycopg2.Error, SystemExit, ValueError, LookupError) as e:
        return report(n, name, False, str(e).splitlines()[0])
    report(n, name, ok, f"{seeded_detail}; real: {detail}")


def real_gate(cur, n, name, fn):
    """Run fn(cur) -> (ok, detail) on the real tables, or report pending when
    the editions table has not been created."""
    if not table_exists(cur):
        return report(n, name, False, NOT_YET, pending=True)
    try:
        ok, detail = fn(cur)
    except (psycopg2.Error, SystemExit, ValueError, LookupError) as e:
        return report(n, name, False, str(e).splitlines()[0])
    report(n, name, ok, detail)


EMPTY = "no live years to check (an empty state is not a pass)"


def _live_years(cur, spec=SPEC):
    cur.execute(f"SELECT DISTINCT reporting_year FROM public.{spec.live_table}"
                " ORDER BY 1")
    return [r[0] for r in cur.fetchall()]


def real_edition1_and_latest(cur, spec=SPEC):
    years = _live_years(cur, spec)
    if not years:
        return False, EMPTY
    cur.execute(f"SELECT DISTINCT reporting_year FROM "
                f"public.{spec.editions_table} WHERE edition = 1 AND "
                "source_file IS NOT NULL")
    have = {r[0] for r in cur.fetchall()}
    missing = [y for y in years if y not in have]
    if missing:
        return False, (f"no edition 1 (with a source_file) for {missing[:8]} "
                       f"({len(missing)} of {len(years)})")
    bad = load_checks.check_latest_equals_live(cur, spec)
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{len(years)} live years each have edition 1; the latest edition "
        "equals live cell for cell")


def real_source_uniform(cur, spec=SPEC):
    years = _live_years(cur, spec)
    if not years:
        return False, EMPTY
    bad = []
    for y in years:
        try:
            tip = core.chain_tip(cur, spec, str(y))
        except (LookupError, ValueError) as e:
            bad.append(str(e))
            continue
        cur.execute(f"SELECT DISTINCT source_file FROM "
                    f"public.{spec.editions_table} WHERE reporting_year = %s "
                    "AND edition = %s", (y, tip))
        want = {r[0] for r in cur.fetchall()}
        cur.execute(f"SELECT DISTINCT source FROM public.{spec.live_table} "
                    "WHERE reporting_year = %s", (y,))
        got = {r[0] for r in cur.fetchall()}
        if len(want) != 1:
            bad.append(f"{y}: tip ed{tip} has source_file {sorted(want)[:2]}")
        elif got != want:
            bad.append(f"{y}: live source {sorted(got, key=str)[:2]} is not "
                       f"uniformly the tip's {sorted(want)[0]!r}")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{len(years)} years: every live row carries the tip edition's "
        "source (the release's data guidance URL)")


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


def real_codes(cur, spec=SPEC):
    """Every code in live and in the editions is in la_boundaries,
    utla_lad_mapping or a declared predecessor; predecessors carry their
    attribution (live and tip); no county council that has one successor
    (resolved to it); no Barnsley or Sheffield new code."""
    years = _live_years(cur, spec)
    if not years:
        return False, EMPTY
    cur.execute("SELECT lad24cd FROM public.la_boundaries")
    valid = {r[0] for r in cur.fetchall()}
    cur.execute("SELECT DISTINCT utla_code FROM public.utla_lad_mapping")
    valid |= {r[0] for r in cur.fetchall()}
    cur.execute("SELECT old_code, new_code FROM public.la_code_lookup WHERE "
                "change_type = 'new_unitary' AND old_code LIKE 'E10%'")
    succ = {}
    for old, new in cur.fetchall():
        succ.setdefault(old, set()).add(new)
    one_to_one = {o for o, n in succ.items() if len(n) == 1}
    bad = []
    for table in (spec.live_table, spec.editions_table):
        cur.execute(f"SELECT DISTINCT lad24cd FROM public.{table}")
        codes = {r[0] for r in cur.fetchall()}
        stray = sorted(c for c in codes
                       if c not in valid and c not in m.PREDECESSORS)
        if stray:
            bad.append(f"{table}: {stray[:4]} not in la_boundaries, "
                       "utla_lad_mapping or the declared predecessors")
        county = sorted(codes & one_to_one)
        if county:
            bad.append(f"{table}: county council(s) {county[:4]} not "
                       "resolved to their unitary")
        new = sorted(codes & {"E08000038", "E08000039"})
        if new:
            bad.append(f"{table}: Barnsley/Sheffield new code(s) {new}")
    for table, extra in ((spec.live_table, ""),
                         (spec.editions_table, " AND edition = (SELECT "
                          "MAX(edition) FROM public." + spec.editions_table
                          + " e2 WHERE e2.reporting_year = t.reporting_year)")):
        cur.execute(f"SELECT COUNT(*) FROM public.{table} t WHERE lad24cd = "
                    "ANY(%s) AND (attribution IS DISTINCT FROM 'predecessor' "
                    "OR successor_codes IS NULL OR attribution_note IS NULL)"
                    + extra, (list(m.PREDECESSORS),))
        n = cur.fetchone()[0]
        if n:
            bad.append(f"{table}: {n} predecessor row(s) without attribution "
                       "(attribution, successor_codes, attribution_note)")
    srcs = _private_dicts(HERE / "s4_care_leaver_editions.py")
    if srcs:
        bad.append(f"private code table in the loader: {srcs[:2]}")
    decl = geography.DATASET_FORM.get(m.RUN_SOURCE, ("?",))[0]
    if decl != "old":
        bad.append(f"source 4 is declared {decl!r}, expected 'old'")
    return not bad, "; ".join(bad[:3]) if bad else (
        "every code in la_boundaries, utla_lad_mapping or a declared "
        "predecessor (attribution complete); counties resolved; no "
        "Barnsley/Sheffield new code; source 4 declared 'old'; no private "
        "dict in the loader")


def real_one_row(cur, spec=SPEC):
    """One row per authority, cohort and year (per edition in the editions
    table); no row whose every count is NULL in live or the tip edition."""
    years = _live_years(cur, spec)
    if not years:
        return False, EMPTY
    bad = []
    for table, grp in ((spec.live_table, "reporting_year"),
                       (spec.editions_table, "reporting_year, edition")):
        cur.execute(f"SELECT COUNT(*) FROM (SELECT 1 FROM public.{table} "
                    f"GROUP BY {grp}, lad24cd, age_group HAVING COUNT(*) > 1)"
                    " q")
        if cur.fetchone()[0]:
            bad.append(f"{table}: an authority twice in one year and cohort")
    allnull = " AND ".join(f"{c} IS NULL" for c in m.COUNT_COLUMNS)
    cur.execute(f"SELECT COUNT(*) FROM public.{spec.live_table} WHERE "
                + allnull)
    if cur.fetchone()[0]:
        bad.append("live: a row whose every count is NULL")
    for y in years:
        tip = core.chain_tip(cur, spec, str(y))
        cur.execute(f"SELECT COUNT(*) FROM public.{spec.editions_table} "
                    "WHERE reporting_year = %s AND edition = %s AND "
                    + allnull, (y, tip))
        if cur.fetchone()[0]:
            bad.append(f"{y} ed{tip}: a row whose every count is NULL")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{len(years)} years: one row per authority, cohort and year in live "
        "and every edition; no all-NULL row in live or the tip editions")


def _tip_rows(cur, spec, cols, where=""):
    """[(year, edition, row)] of every year's tip edition and of live
    ((year, None, row))."""
    out = []
    for y in _live_years(cur, spec):
        tip = core.chain_tip(cur, spec, str(y))
        cur.execute(f"SELECT lad24cd, age_group, {', '.join(cols)} FROM "
                    f"public.{spec.editions_table} WHERE reporting_year = %s "
                    f"AND edition = %s {where}", (y, tip))
        out += [(y, tip, r) for r in cur.fetchall()]
    cur.execute(f"SELECT reporting_year, lad24cd, age_group, "
                f"{', '.join(cols)} FROM public.{spec.live_table} "
                f"WHERE true {where}")
    out += [(r[0], None, r[1:]) for r in cur.fetchall()]
    return out


def real_null_vs_zero(cur, spec=SPEC):
    """In live and the tip editions: a NULL built column has its reason in
    null_reasons and a column named there is NULL; unsuitable_pct is never
    written; the latest edition equals live (0 is not NULL)."""
    years = _live_years(cur, spec)
    if not years:
        return False, EMPTY
    bad = load_checks.check_latest_equals_live(cur, spec)
    cols = tuple(m.BUILT["17-21"] + m.BUILT["22-25"])
    cols = tuple(dict.fromkeys(cols))
    n = 0
    for y, ed, r in _tip_rows(cur, spec, cols + ("null_reasons",
                                                  "unsuitable_pct")):
        lad, age, vals = r[0], r[1], dict(zip(cols, r[2:2 + len(cols)]))
        reasons, pct = r[2 + len(cols)], r[3 + len(cols)]
        named = dict(p.split("=", 1) for p in (reasons or "").split(";") if p)
        where = f"{y} {'ed' + str(ed) if ed else 'live'} {lad} {age}"
        if pct is not None:
            bad.append(f"{where}: unsuitable_pct written")
        for c in m.BUILT[age]:
            n += 1  # not a source value
            if (vals[c] is None) != (c in named):
                bad.append(f"{where}: {c}="
                           f"{vals[c] if vals[c] is not None else 'NULL'} "
                           f"but null_reasons {reasons!r}")
        if len(bad) > 6:
            break
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{n:,} built cells in live and the tip editions: NULL exactly where "
        "null_reasons says so, unsuitable_pct never written, latest edition "
        "equals live")


def real_built_null(cur, spec=SPEC):
    """A built column is NULL when a part is NULL, in live and the tip
    editions: semi_independent when foyers, supported_lodgings or
    semi_independent_published is NULL; total_care_leavers when any bucket
    (17-21) or suitable, unsuitable or not_known (22-25) is NULL; and equal
    to the sum when all are present."""
    years = _live_years(cur, spec)
    if not years:
        return False, EMPTY
    buckets = tuple(c for c, _ in m.BUCKETS_1721)
    cols = tuple(dict.fromkeys(buckets + m.BUILT["22-25"]
                               + ("foyers", "supported_lodgings",
                                  "semi_independent_published")))
    bad, n = [], 0
    for y, ed, r in _tip_rows(cur, spec, cols):
        age, v = r[1], dict(zip(cols, r[2:]))
        where = f"{y} {'ed' + str(ed) if ed else 'live'} {r[0]} {age}"
        if age == "17-21":
            parts = [v[c] for c in buckets]
            semi_parts = [v["foyers"], v["supported_lodgings"],
                          v["semi_independent_published"]]
            if any(p is None for p in semi_parts) and \
                    v["semi_independent"] is not None:
                bad.append(f"{where}: semi_independent built from a NULL "
                           "part")
        else:
            parts = [v["suitable_count"], v["unsuitable"], v["not_known"]]
        n += 1  # not a source value
        if any(p is None for p in parts):
            if v["total_care_leavers"] is not None:
                bad.append(f"{where}: total_care_leavers built from a NULL "
                           "part")
        elif v["total_care_leavers"] != sum(parts):
            bad.append(f"{where}: total_care_leavers "
                       f"{v['total_care_leavers']} is not the sum {sum(parts)}")
        if len(bad) > 6:
            break
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{n:,} rows: built columns are NULL when a part is NULL and "
        "total_care_leavers is the sum of its parts when all are published")


def real_sums_22_25(cur, spec=SPEC):
    """22-25 rows hold the four ages summed: every authority's 22-25 total is
    at least its 17-21 neighbour's order of magnitude is not claimed; what is
    checked is that no 22-25 row equals a single age (a published 22-25 total
    is the sum over four ages, so it exceeds the count of any one age band
    only when more than one is published) and that county councils are held
    for 22-25."""
    years = [y for y in _live_years(cur, spec)]
    if not years:
        return False, EMPTY
    cur.execute(f"SELECT COUNT(DISTINCT reporting_year), COUNT(*) FILTER "
                f"(WHERE lad24cd LIKE 'E10%') FROM public.{spec.live_table} "
                "WHERE age_group = '22-25'")
    n_years, counties = cur.fetchone()
    cur.execute(f"SELECT COUNT(*) FROM public.{spec.live_table} WHERE "
                "age_group = '22-25' AND total_published IS NOT NULL AND "
                "total_care_leavers IS NOT NULL AND "
                "total_care_leavers <> total_published")
    off = cur.fetchone()[0]
    cur.execute(f"SELECT COUNT(*) FROM public.{spec.live_table} WHERE "
                "age_group = '22-25'")
    rows = cur.fetchone()[0]
    bad = []
    if not rows:
        bad.append("no 22-25 rows in live")
    if off:
        bad.append(f"{off} live 22-25 rows where the sum of the parts "
                   "differs from the published total")
    if rows and not counties:
        bad.append("no county council in the 22-25 rows (the age-25-only "
                   "build dropped them)")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{rows} live 22-25 rows over {n_years} years, {counties} county "
        "council rows; the summed parts equal the published total wherever "
        "both are published")


# ---------------------------------------------------------------- gate 4

def seeded_codes(cur):
    """Seeded: predecessor attribution, counties resolved, new code and
    undeclared code halt."""
    out = {}

    def pred(e):
        r = e.release("2019", (2019,), (2019,), codes17=CODES + ["E06000028"])
        e.latest = "2019"
        rc, text, _, _ = e.run_main(["load", "--release", "2019", "--commit"])
        rows = ed_rows(e.cur, 2019, 1) if rc == 0 else {}
        row = rows.get(("E06000028", "17-21"), {})
        out["predecessor"] = (rc == 0
                              and row.get("attribution") == "predecessor"
                              and row.get("successor_codes") == ["E06000058"]
                              and "Bournemouth" in (row.get("attribution_note")
                                                    or "")
                              and ("E06000058", "17-21") not in rows)
        return r

    def county(e):
        e.release("2024", (2024,), (2024,), codes22=CODES + ["E10000023"])
        e.latest = "2024"
        rc, text, _, _ = e.run_main(["load", "--release", "2024", "--commit"])
        rows = ed_rows(e.cur, 2024, 1) if rc == 0 else {}
        out["county"] = (rc == 0 and ("E06000065", "22-25") in rows
                         and ("E10000023", "22-25") not in rows
                         and "E10000023 -> E06000065" in text)

    def bad_code(code, needle):
        def f(e):
            e.release("2024", (2024,), (2024,), codes17=CODES + [code])
            e.latest = "2024"
            before = state(e.cur)
            ok, text = halts(e, ["load", "--release", "2024", "--commit"],
                             before)
            out[code] = ok and needle in text
        return f
    for fn in (pred, county, bad_code("E08000038", "declared 'old'"),
               bad_code("E06000099", "UNEXPLAINED E06000099")):
        scenario(cur, fn, seed=False)
    return out


def gate_4_codes(cur):
    name = ("codes: la_boundaries, utla_lad_mapping or a declared "
            "predecessor; counties resolved; no Barnsley/Sheffield new code; "
            "no private dict")
    try:
        res = seeded_codes(cur)
    except (psycopg2.Error, SystemExit, RuntimeError) as ex:
        return report(4, name, False, str(ex).splitlines()[0])
    bad = [k for k, v in res.items() if not v]
    mixed(4, name, not bad,
          f"predecessor attribution kept on its own code="
          f"{res.get('predecessor')}, county resolved to its unitary="
          f"{res.get('county')}, E08000038 halts={res.get('E08000038')}, an "
          f"undeclared code halts={res.get('E06000099')}" if not bad else
          f"failed: {bad}", cur, real_codes)


# ---------------------------------------------------------------- gate 5

def gate_5_one_row(cur):
    name = ("one row per authority, cohort and year; no all-NULL row; an "
            "all-z row not stored; two codes to one key halt")
    out = {}

    def allz(e):
        e.release("2025", (2025,), (2025,), allz17=[(CODES[3], 2025)])
        e.latest = "2025"
        rc, text, _, _ = e.run_main(["load", "--release", "2025", "--commit"])
        rows = ed_rows(e.cur, 2025, 1) if rc == 0 else {}
        e.cur.execute(f"SELECT COUNT(*) FROM public.{ZL} WHERE lad24cd = %s "
                      "AND reporting_year = 2025 AND age_group = '17-21'",
                      (CODES[3],))
        in_live = e.cur.fetchone()[0]
        out["allz"] = (rc == 0 and (CODES[3], "17-21") not in rows
                       and (CODES[3], "22-25") in rows and in_live == 0
                       and "1 all-z rows not stored" in text
                       and len(rows) == 2 * N - 1)

    def two(e):
        e.release("2024", (2024,), (2024,),
                  codes22=CODES + ["E10000023", "E06000065"])
        e.latest = "2024"
        before = state(e.cur)
        ok, text = halts(e, ["load", "--release", "2024", "--commit"],
                         before)
        out["two"] = ok and "E06000065" in text
    try:
        scenario(cur, allz, seed=False)
        scenario(cur, two, seed=False)
    except (psycopg2.Error, SystemExit, RuntimeError) as ex:
        return report(5, name, False, str(ex).splitlines()[0])
    mixed(5, name, all(out.values()),
          f"all-z row not stored and counted={out.get('allz')}; two codes to "
          f"one key halt={out.get('two')}", cur, real_one_row)


# ---------------------------------------------------------------- gate 6

def parts_of(cohort, col, schema="care_leaver_2025"):
    """The category labels (of the 2025-schema files) that feed a built
    column."""
    cs = m.SCHEMAS[schema]["cohorts"][cohort]
    if cohort == "17-21":
        if col == "total_care_leavers":
            return [c for _, cats in m.BUCKETS_1721 for c in cats]
        return list(dict(m.BUCKETS_1721 + m.PARTS_1721)[col])
    inv = {v: k for k, v in cs["categories"].items()}
    canon = (m.TOTAL_2225 if col == "total_care_leavers"
             else dict(m.COLS_2225)[col])
    return [inv[c] for c in canon]


def affected(cohort, cat):
    """The built columns a category feeds."""
    return {c for c in m.BUILT[cohort] if cat in parts_of(cohort, c)}


def ages_of(cohort):
    return labels("care_leaver_2025", cohort)[0]


def plant_run(e, plants17, plants22, slug="2025", year=2025):
    """Load one new year with the given {(code, age, category): text} plants
    for each cohort; returns (rc, text)."""
    v17 = {(c, year, a, cat): t for (c, a, cat), t in plants17.items()}
    v22 = {(c, year, a, cat): t for (c, a, cat), t in plants22.items()}
    e.release(slug, (year,), (year,), values17=v17, values22=v22)
    e.latest = slug
    rc, text, _, _ = e.run_main(["load", "--release", slug, "--commit"])
    return rc, text


def seeded_null_vs_zero(cur):
    """Every built column, both cohorts: a c in one part is NULL with its
    reason and affects exactly the columns the category feeds; a published 0
    in every part stays 0; k and a blank halt."""
    problems = []
    cols17, cols22 = list(m.BUILT["17-21"]), list(m.BUILT["22-25"])
    for i in range(len(cols17)):
        c17, c22 = cols17[i], cols22[i % len(cols22)]

        def body(e, c17=c17, c22=c22):
            a, b = CODES[0], CODES[1]
            plants = {}
            for cohort, col in (("17-21", c17), ("22-25", c22)):
                cats = parts_of(cohort, col)
                ages = ages_of(cohort)
                plants[cohort] = {(a, ages[0], cats[0]): "c"}
                for ag in ages:
                    for cat in cats:
                        plants[cohort][(b, ag, cat)] = "0"
            rc, text = plant_run(e, plants["17-21"], plants["22-25"])
            if rc != 0:
                problems.append(f"{c17}/{c22}: load failed: {text[-120:]}")
                return
            for cohort, col in (("17-21", c17), ("22-25", c22)):
                rows = ed_rows(e.cur, 2025, 1, cohort)
                ra, rb = rows[(CODES[0], cohort)], rows[(CODES[1], cohort)]
                cat = parts_of(cohort, col)[0]
                want = affected(cohort, cat)
                got = {c for c in m.BUILT[cohort] if ra[c] is None}
                if got != want:
                    problems.append(f"{cohort} c in {cat!r}: NULL columns "
                                    f"{sorted(got)}, expected {sorted(want)}")
                if not all(f"{c}=suppressed" in (ra["null_reasons"] or "")
                           for c in want):
                    problems.append(f"{cohort} {col}: reason missing: "
                                    f"{ra['null_reasons']}")
                if rb[col] != 0 or f"{col}=" in (rb["null_reasons"] or ""):
                    problems.append(f"{cohort} published 0 in every part of "
                                    f"{col} became {rb[col]!r} "
                                    f"({rb['null_reasons']})")
            e.cur.execute(f"SELECT COUNT(*) FROM public.{ZL} WHERE lad24cd = "
                          "%s AND reporting_year = 2025 AND age_group = "
                          "'17-21' AND " + c17 + " IS NULL", (CODES[0],))
            if e.cur.fetchone()[0] != 1 or core.rows_differing(
                    e.cur, ZZ, "2025", 1):
                problems.append(f"{c17}: live differs from edition 1")
        scenario(cur, body, seed=False)

    def markers(e):
        for text in ("k", "", "N/A", "-1", "1.5", "1,234"):
            plants = {(CODES[0], Y17, "Foyers"): text}
            e.release("2025", (2025,), None, values17={
                (c, 2025, a, cat): t for (c, a, cat), t in plants.items()})
            e.latest = "2025"
            before = state(e.cur)
            ok, out = halts(e, ["load", "--release", "2025", "--commit"],
                            before)
            if not ok:
                problems.append(f"count value {text!r} did not halt")
    scenario(cur, markers, seed=False)
    return problems


def gate_6_null_vs_zero(cur):
    name = ("NULL versus 0 never conflated (seeded per built column: c is "
            "NULL with its reason, a published 0 stays 0, k and blank halt)")
    try:
        problems = seeded_null_vs_zero(cur)
    except (psycopg2.Error, SystemExit, RuntimeError, KeyError) as ex:
        return report(6, name, False, f"{type(ex).__name__}: {ex}")
    n = len(m.BUILT["17-21"]) + len(m.BUILT["22-25"])
    mixed(6, name, not problems,
          f"{n} built columns (both cohorts) each checked with a c and with "
          "a published 0; k, blank, N/A, negative, decimal and thousands "
          "separator halt" if not problems else "; ".join(problems[:3]),
          cur, real_null_vs_zero)


# ---------------------------------------------------------------- gate 7

def gate_7_built_null(cur):
    name = ("built columns NULL when a part is NULL, total_care_leavers "
            "likewise; z, x and mixed reasons (seeded)")
    problems = []

    def body(e):
        a, b, c = CODES[0], CODES[1], CODES[2]
        p17 = {(a, Y17, m.SEMI): "z", (a, Y17, "Foyers"): "x",
               (b, Y22, "Supported lodgings"): "c"}
        ages22 = ages_of("22-25")
        suit = parts_of("22-25", "suitable_count")[0]
        p22 = {(c, ages22[2], suit): "c"}
        rc, text = plant_run(e, p17, p22)
        if rc != 0:
            problems.append(f"load failed: {text[-150:]}")
            return
        r17, r22 = ed_rows(e.cur, 2025, 1, "17-21"), ed_rows(
            e.cur, 2025, 1, "22-25")
        ra, rb = r17[(a, "17-21")], r17[(b, "17-21")]
        rc_ = r22[(c, "22-25")]
        why = dict(p.split("=") for p in ra["null_reasons"].split(";"))
        if why != {"semi_independent": "mixed",
                   "semi_independent_published": "not_applicable",
                   "foyers": "not_available",
                   "total_care_leavers": "mixed"}:
            problems.append(f"z and x reasons: {why}")
        if rb["semi_independent"] is not None or rb["supported_lodgings"] \
                is not None or rb["total_care_leavers"] is not None \
                or rb["suppressed_flag"] is not True \
                or rb["semi_independent_published"] is None \
                or rb["foyers"] is None:
            problems.append("one suppressed part of the semi-independent "
                            "bucket did not null exactly the bucket, "
                            "supported_lodgings and the total, or did not "
                            f"set suppressed_flag: {rb['null_reasons']}")
        if rc_["suitable_count"] is not None or rc_["total_care_leavers"] \
                is not None or rc_["unsuitable"] is None \
                or rc_["not_known"] is None:
            problems.append("one suppressed age of four did not null "
                            f"suitable_count and the total: "
                            f"{rc_['null_reasons']}")
        if rc_["suppressed_flag"] is not None:
            problems.append("22-25 suppressed_flag is not NULL")
        if rb["unsuitable_pct"] is not None:
            problems.append("unsuitable_pct written")
    try:
        scenario(cur, body, seed=False)
    except (psycopg2.Error, SystemExit, RuntimeError, KeyError) as ex:
        return report(7, name, False, f"{type(ex).__name__}: {ex}")
    mixed(7, name, not problems,
          "z/x/c parts: reasons not_applicable, not_available, mixed, "
          "suppressed; the bucket, its parts' sums and total_care_leavers "
          "NULL, unaffected columns kept; 22-25 likewise"
          if not problems else "; ".join(problems[:3]), cur, real_built_null)


# ---------------------------------------------------------------- gate 8

def gate_8_sums_22_25(cur):
    name = "22-25 sums all four ages (not age 25 alone) (seeded)"
    problems = []

    def body(e):
        ages = ages_of("22-25")
        suit = parts_of("22-25", "suitable_count")[0]
        unsu = parts_of("22-25", "unsuitable")[0]
        info = parts_of("22-25", "not_known")[0]
        tot = parts_of("22-25", "total_published")[0]
        v = {}
        for i, a in enumerate(ages):          # Liverpool-style: 22 .. 25
            v[(CODES[0], a, suit)] = str(100 * (i + 1))
            v[(CODES[0], a, unsu)] = str(10 * (i + 1))
            v[(CODES[0], a, info)] = str(i + 1)
            v[(CODES[0], a, tot)] = str(111 * (i + 1))
        rc, text = plant_run(e, {}, v)
        if rc != 0:
            problems.append(f"load failed: {text[-150:]}")
            return
        row = ed_rows(e.cur, 2025, 1, "22-25")[(CODES[0], "22-25")]
        want = {"suitable_count": 1000, "unsuitable": 100, "not_known": 10,
                "total_published": 1110, "total_care_leavers": 1110}
        got = {k: row[k] for k in want}
        if got != want:
            problems.append(f"summed {got}, expected {want} (age 25 alone "
                            "would give 400/40/4/444)")
        # an age missing from the file halts
        e.release("2026", None, (2026,))
        e.latest = "2026"
        edit_csv(e.paths[("2026", "22-25")],
                 lambda rows: [r for r in rows
                               if r[10] != "24 years"])
        before = state(e.cur)
        ok, text = halts(e, ["load", "--release", "2026", "--commit"], before)
        if not ok:
            problems.append("a file without one of the four ages did not "
                            "halt")
    try:
        scenario(cur, body, seed=False)
    except (psycopg2.Error, SystemExit, RuntimeError, KeyError) as ex:
        return report(8, name, False, f"{type(ex).__name__}: {ex}")
    mixed(8, name, not problems,
          "four distinct ages summed (1,000/100/10, total 1,110), not age 25 "
          "alone; a file missing an age halts" if not problems else
          "; ".join(problems[:3]), cur, real_sums_22_25)


# ---------------------------------------------------------------- gate 9

def gate_9_identity(cur):
    name = ("identity: schema, categories, ages, time_identifier, level, "
            "grid, rows, range and release; each seeded mismatch halts "
            "before any write")
    results = {}

    def case(label, mutate_page=None, mutate_file=None, cohort="17-21",
             argv=None, setup=None):
        def body(e):
            e.release("2024", range(2020, 2025), (2023, 2024))
            e.latest = "2024"
            if mutate_page:
                sets = e.pages["2024"]["props"]["pageProps"]["dataContent"][
                    "dataSets"]
                mutate_page(sets)
            if mutate_file:
                edit_csv(e.paths[("2024", cohort)], mutate_file)
            if setup:
                setup(e)
            before = state(e.cur)
            ok, text = halts(e, argv or ["load", "--release", "2024",
                                         "--commit"], before)
            results[label] = (ok, text)
        scenario(cur, body, seed=False)

    def rename(i, old, new):
        def f(rows):
            return [[new if (j > 0 and c == old and k == i) else c
                     for k, c in enumerate(r)] for j, r in enumerate(rows)]
        return f

    def header(rows):
        rows[0] = [("count" if c == "number" else c) for c in rows[0]]
        return rows

    def drop(rows):
        return rows[:5] + rows[6:]

    def dup(rows):
        return rows + [rows[-1]]

    case("page rows +1", mutate_page=lambda s: s[0]["meta"].update(
        numDataFileRows=s[0]["meta"]["numDataFileRows"] + 1))
    case("page range", mutate_page=lambda s: s[0]["meta"].update(
        timePeriodRange={"start": "2019", "end": "2024"}))
    case("unknown header", mutate_file=header)
    case("time_identifier", mutate_file=rename(1, "Reporting year",
                                               "Calendar year"))
    case("unknown category", mutate_file=rename(11, "Foyers", "Hostels"))
    case("unknown age", mutate_file=rename(10, "17 to 18 years", "17 years"))
    case("unknown level", mutate_file=rename(2, "Local authority",
                                             "Local authority district"))
    case("incomplete grid", mutate_file=drop)
    case("duplicate row", mutate_file=dup)
    case("2024 22-25 wording", cohort="22-25",
         mutate_file=rename(10, "Aged 22", "Age 22"))

    def other_slug(e):
        e.release("2024", range(2020, 2025), (2023, 2024))
        e.pages["2026"] = e.pages["2024"]
        e.latest = "2026"
        before = state(e.cur)
        ok, text = halts(e, ["load", "--commit"], before)
        results["page slug"] = (ok and "data guidance page is release 2024"
                                in text, text)

    def mixed_release(e):
        a = e.release("2024", range(2020, 2025), None, register=False)
        b = e.release("2025", None, (2023, 2024, 2025), register=False)
        before = state(e.cur)
        ok, text = halts(e, ["load", "--release", "2025", "--file-17-21",
                             str(a["17-21"]), "--file-22-25",
                             str(b["22-25"]), "--commit"], before)
        results["two releases"] = (ok and "own latest year is 2024, the "
                                   "release is 2025" in text, text)

    def file_year(e):
        a = e.release("2024", range(2020, 2025), None, register=False)
        before = state(e.cur)
        ok, text = halts(e, ["load", "--release", "2025", "--file-17-21",
                             str(a["17-21"]), "--commit"], before)
        results["file's own year is not the release's"] = (
            ok and "own latest year is 2024, the release is 2025" in text,
            text)

    def cohort_swap(e):
        a = e.release("2024", range(2020, 2025), (2023, 2024),
                      register=False)
        before = state(e.cur)
        ok, text = halts(e, ["load", "--release", "2024", "--file-17-21",
                             str(a["22-25"]), "--commit"], before)
        results["22-25 file given as 17-21"] = (ok, text)
    for fn in (other_slug, mixed_release, file_year, cohort_swap):
        scenario(cur, fn, seed=False)
    bad = [k for k, (ok, _) in results.items() if not ok]
    report(9, name, not bad,
           f"{len(results)} seeded mismatches each halted with nothing "
           "stored, no commit and no run-log row" if not bad else
           f"did not halt cleanly: {bad}")


# --------------------------------------------------------------- gate 10

def gate_10_older_release(cur):
    name = ("older-release guard on the page, --release (dataset ids), "
            "--file and default paths, ranked by the file's own latest "
            "year, never its name")
    problems = []

    def body(e):
        # held: 2019-2023 from the 2023 release, 2021-2025 from the 2025
        # release (tips of 2021-2025 are the 2025 release). The 2024
        # release's files carry a name that claims a newer year.
        e.seed_three()
        e.fname = "dfe_cla_2099_{cohort}.csv"
        r = e.release("2024", range(2021, 2025), (2023, 2024),
                      values17={(CODES[0], 2022, Y17, "Foyers"): "4"})
        before = state(e.cur)
        paths = (
            ["load", "--release", "2024", "--commit"],
            ["load", "--release", "2024", "--commit",
             "--dataset-17-21", e.ids[("2024", "17-21")],
             "--dataset-22-25", e.ids[("2024", "22-25")]],
            ["load", "--release", "2024", "--commit", "--file-17-21",
             str(r["17-21"]), "--file-22-25", str(r["22-25"])],
            ["load", "--release", "2024"])
        for argv in paths:
            ok, text = halts(e, argv, before)
            if not (ok and "every year in the files is skipped" in text
                    and "--allow-older-file" in text):
                problems.append(f"{argv[1:5]} stored or did not halt: "
                                f"{text[-160:]}")
        # the default path: the latest release older than held halts
        ok, text = halts(e, ["load", "--commit"], before, latest="2024")
        if not (ok and "older than the held latest year 2025" in text):
            problems.append("default path with an older latest release did "
                            "not halt")
        # --allow-older-file stores and says so
        rc, text, _, logged = e.run_main(
            ["load", "--release", "2024", "--commit", "--allow-older-file"])
        if rc != 0 or "NOTE: --allow-older-file given" not in text \
                or "--allow-older-file" not in logged.call_args[0][2]:
            problems.append("--allow-older-file did not store and log")
        # per year: a 2024 release also covering 2020 (held from 2023 only)
        # compares 2020 and skips the years held from 2025
    scenario(cur, body, seed=False)

    def per_year(e):
        e.seed_three()
        e.fname = "dfe_cla_2099_{cohort}.csv"
        e.release("2024", range(2020, 2025), (2023, 2024),
                  values17={(CODES[0], 2020, Y17, "Foyers"): "4",
                            (CODES[0], 2022, Y17, "Foyers"): "4"})
        rc, text, _, _ = e.run_main(["load", "--release", "2024", "--commit"])
        eds = lambda y: tl.editions(e.cur, y)  # noqa: E731
        if rc != 0 or "compare [2020]" not in text or \
                [len(eds(y)) for y in (2020, 2021, 2022, 2023, 2024)] != \
                [2, 1, 1, 2, 1] or any(
                    f"{y} (older: its tip is from the 2025 release" not in text
                    for y in (2021, 2022, 2023, 2024)):
            problems.append("a 2024 release did not compare only the year "
                            f"held from 2023 (2020): {text[-250:]}")
    scenario(cur, per_year, seed=False)
    report(10, name, not problems,
           "a 2024 release over years held from the 2025 release is "
           "skipped per year on the page, dataset-id, --file and preview "
           "paths with files named for 2099; the default path and "
           "--allow-older-file behave as documented" if not problems else
           "; ".join(problems[:3]))


# --------------------------------------------------------------- gate 11

def gate_11_stop_conditions(cur):
    name = ("stop conditions: each seeded REJECTED and stores nothing; the "
            "limits themselves pass")
    problems = []
    yrs = range(2021, 2026)

    def run(e, label, needle, year, newer, rng=(1, 100), patches=(),
            extra=("--commit",), expect_rc=1):
        e.release("2025", **newer)
        e.latest = "2025"
        snap = (tl.editions(e.cur, year), tl.ledger(e.cur, year),
                ed_rows(e.cur, year) if tl.editions(e.cur, year) else {},
                live_year(e.cur, year))
        rc, text, _, logged = e.run_main(["load", *extra], rng=rng,
                                         patches=patches)
        if expect_rc == 1:
            now = (tl.editions(e.cur, year), tl.ledger(e.cur, year),
                   ed_rows(e.cur, year) if tl.editions(e.cur, year) else {},
                   live_year(e.cur, year))
            if not (rc == 1 and needle in text and "REJECTED" in text
                    and now == snap and not logged.called):
                problems.append(f"{label}: rc {rc}, nothing-stored "
                                f"{now == snap}: {text[-200:]}")
        elif rc != 0 or needle not in text:
            problems.append(f"{label}: rc {rc}: {text[-200:]}")

    def live_year(cur, year):
        cur.execute(f"SELECT md5(string_agg(t::text, ',' ORDER BY t::text)) "
                    f"FROM public.{ZL} t WHERE reporting_year = %s", (year,))
        return cur.fetchone()[0]

    def newer(**kw):
        d = dict(years17=yrs, years22=(2023, 2024, 2025))
        d.update(kw)
        return d

    def big(n):
        return {(c, 2023, Y17, "Total"): "90" for c in CODES[:n]}

    def b1(e):
        run(e, "total_published >25% in 4 authorities",
            "total_published changes by more than 25% in 4 authorities",
            2023, newer(values17=big(4)))
    scenario(cur, b1)

    def b2(e):
        run(e, "national total_published +5%",
            "national total_published", 2023, newer(values17=big(3)))
    scenario(cur, b2)

    def b3(e):
        # the limits themselves pass: 3 authorities changing >25%, with the
        # national limit lifted for this case
        run(e, "3 authorities over 25% passes", "2023: revised", 2023,
            newer(values17=big(3)),
            patches=(mock.patch.object(m, "NATIONAL_CHANGE", 0.9),),
            expect_rc=0)
        if len(tl.editions(e.cur, 2023)) != 2:
            problems.append("3 authorities over 25% did not store an "
                            "edition at the limit")
    scenario(cur, b3)

    def b4(e):
        run(e, "held authority with a published number absent",
            "held authorit", 2023, newer(codes17=CODES[:-1],
                                         codes22=CODES))
    scenario(cur, b4)

    def b5(e):
        run(e, "new year outside the authority range",
            "authorities, expected 145-160", 2025, newer(),
            rng=m.AUTHORITIES_RANGE)
    scenario(cur, b5)

    def b6(e):
        e.release("2025", range(2021, 2026), None, register=False)
        # a one-cohort release against a two-cohort tip
        r = e.release("2026", range(2021, 2026), None, register=False)
        snap = (tl.editions(e.cur, 2023), tl.editions(e.cur, 2024))
        rc, text, _, logged = e.run_main(
            ["load", "--release", "2025", "--file-17-21",
             str(r["17-21"]), "--commit"])
        if not (rc == 1 and "no carry-forward" in text and "REJECTED" in text
                and (tl.editions(e.cur, 2023), tl.editions(e.cur, 2024))
                == snap and not logged.called):
            problems.append(f"one cohort against two: rc {rc}: {text[-200:]}")
    scenario(cur, b6)

    def b7(e):
        zeros = {(CODES[2], 2023, a, c): "0"
                 for a in (Y17, Y22)
                 for c in ("Bed and breakfast", "Emergency accommodation",
                           "No fixed abode/homeless")}
        e.seed(values17=zeros)
        flip = dict(zeros)
        flip[(CODES[2], 2023, Y17, "Bed and breakfast")] = "c"
        run(e, "0 to NULL without --acknowledge", "0 to NULL", 2023,
            newer(values17=flip))
    scenario(cur, b7, seed=False)
    report(11, name, not problems,
           "total_published >25% in 4 authorities, national +5%, a held "
           "authority absent, new-year authority range, one cohort against "
           "two, and 0 to NULL each REJECTED with exit 1 and nothing "
           "stored; 3 authorities over 25% is accepted" if not problems else
           "; ".join(problems[:3]))


# --------------------------------------------------------------- gate 12

def gate_12_one_transaction(cur):
    name = ("a new year in one transaction (edition 1, live rows and two "
            "ledger rows); a failing live or ledger write rolls all back")
    problems = []

    def body(e):
        e.release("2025", (2025,), (2025,))
        e.latest = "2025"
        for label, patch in (
                ("live insert", mock.patch.object(
                    m, "insert_live", side_effect=RuntimeError("boom"))),
                ("ledger write", mock.patch.object(
                    pe, "record_file_check",
                    side_effect=RuntimeError("boom")))):
            before = (tl.editions(e.cur, 2025), tl.ledger(e.cur, 2025),
                      tl.count(e.cur, ZL, "WHERE reporting_year = 2025"))
            with patch:
                rc, text, _, logged = e.run_main(["load", "--commit"])
            now = (tl.editions(e.cur, 2025), tl.ledger(e.cur, 2025),
                   tl.count(e.cur, ZL, "WHERE reporting_year = 2025"))
            if not (rc == 1 and "2025: FAILED" in text and now == before
                    and not logged.called):
                problems.append(f"failing {label} left {now}: {text[-120:]}")
        rc, text, _, logged = e.run_main(["load", "--commit"])
        led = tl.ledger(e.cur, 2025)
        if rc != 0 or tl.editions(e.cur, 2025) != [(1, None, 2 * N)] or \
                tl.count(e.cur, ZL, "WHERE reporting_year = 2025") != 2 * N \
                or [(o, ed) for _, o, ed in led] != [("new", 1), ("new", 1)] \
                or {s for s, _, _ in led} != {
                    m.ledger_source(m.csv_url(e.ids[("2025", c)]), "2025")
                    for c in ("17-21", "22-25")} \
                or core.rows_differing(e.cur, ZZ, "2025", 1) \
                or not logged.called:
            problems.append(f"a new year did not store edition 1, live and "
                            f"two ledger rows together: {text[-150:]}")
        if not m.status(e.cur, ZZ)["ok"]:
            problems.append("status not clean after the load")
    scenario(cur, body)
    report(12, name, not problems,
           "edition 1, 2N live rows and two ledger rows stored together; a "
           "failing live insert and a failing ledger write each roll all "
           "of them back" if not problems else "; ".join(problems[:3]))


# --------------------------------------------------------------- gate 13

def gate_13_ledger(cur):
    name = ("ledger: a file pair already in it is not parsed again; an "
            "unchanged recheck is recorded once; a changed sha is read")
    problems = []

    def body(e):
        led = tl.ledger(e.cur)
        real = m.read_file
        calls = []
        with mock.patch.object(m, "read_file",
                               side_effect=lambda p: calls.append(p)
                               or real(p)):
            rc, text, _, logged = e.run_main(["load", "--release", "2024",
                                              "--commit"])
        if rc != 0 or calls or "nothing parsed" not in text or \
                tl.ledger(e.cur) != led or logged.called:
            problems.append(f"known (URL, sha256) was parsed or recorded "
                            f"again: {len(calls)} reads")
        rc, text, _, logged = e.run_main(["load", "--release", "2024",
                                          "--recheck", "2024"])
        if rc != 0 or "2024: unchanged" not in text or tl.ledger(e.cur) != led:
            problems.append("preview recheck wrote or did not read")
        rc, text, _, logged = e.run_main(["load", "--release", "2024",
                                          "--recheck", "2024", "--commit"])
        got = [(o, ed) for _, o, ed in tl.ledger(e.cur, 2024)]
        if rc != 0 or got != [("new", 1), ("new", 1), ("unchanged", 1),
                              ("unchanged", 1)] or not logged.called:
            problems.append(f"recheck --commit ledger {got}")
        # the same URL with a changed sha is read again
        edit_csv(e.paths[("2024", "17-21")], lambda rows: rows + [])
        p = e.paths[("2024", "17-21")]
        text0 = p.read_text(encoding="utf-8")
        p.write_text(text0 + "\n", encoding="utf-8")
        calls.clear()
        with mock.patch.object(m, "read_file",
                               side_effect=lambda q: calls.append(q)
                               or real(q)):
            rc, text, _, _ = e.run_main(["load", "--release", "2024"])
        if not calls:
            problems.append("a changed sha256 for a known URL was not read")
    scenario(cur, body)
    report(13, name, not problems,
           "nothing parsed for a known (URL, sha256); a recheck adds ledger "
           "rows only with --commit; a changed sha is read" if not problems
           else "; ".join(problems[:3]))


# --------------------------------------------------------------- gate 14

def gate_14_preview_writes_nothing(cur):
    name = ("preview, --simulate and the migrate-legacy preview write "
            "nothing (no editions, ledger, live or run-log rows, no commit)")
    problems = []

    def body(e):
        e.release("2025", range(2021, 2026), (2023, 2024, 2025),
                  values17={(CODES[1], 2023, Y17, "Foyers"): "5"})
        e.latest = "2025"
        before = state(e.cur)
        for argv in (["load"], ["load", "--simulate"], ["refresh-latest"],
                     ["restore-edition", "2023", "1"]):
            rc, text, b, logged = e.run_main(argv)
            if argv[0] == "restore-edition":
                ok = rc == "halt" or rc == 0
            else:
                ok = rc == 0
            if not (ok and state(e.cur) == before and b.commits == 0
                    and not logged.called):
                problems.append(f"{argv} wrote: rc {rc}")
    scenario(cur, body)

    def legacy(e, files):
        before = (state(e.cur), m.live_state(e.cur, ZZ))
        for argv in (("--simulate",), ()):
            rc, text, b, logged = e.migrate_cmd(files, argv)
            if not (rc == 0 and ("PREVIEW" in text or "SIMULATION" in text)
                    and (state(e.cur), m.live_state(e.cur, ZZ)) == before
                    and not logged.called):
                problems.append(f"migrate-legacy {argv or 'preview'} wrote: "
                                f"rc {rc}: {text[-150:]}")
    legacy_world(cur, legacy)
    report(14, name, not problems,
           "load, load --simulate, refresh-latest, restore-edition and "
           "migrate-legacy (preview and --simulate) left live, editions, "
           "ledger and run log as they were" if not problems else
           "; ".join(problems[:3]))


# --------------------------------------------------------------- gate 15

def gate_15_revision_and_refresh(cur):
    name = ("revision then refresh-latest changes only that year, moves "
            "source on every row and loaded_at")
    problems = []

    def body(e):
        before = {y: tl.live(e.cur, y) for y in range(2020, 2025)}
        src = {y: tl.live_sources(e.cur, y) for y in range(2020, 2025)}
        e.release("2025", range(2021, 2026), (2023, 2024, 2025),
                  values17={(CODES[1], 2023, Y17, "Foyers"): "5"})
        e.latest = "2025"
        rc, text, _, _ = e.run_main(["load", "--commit"])
        if rc != 0 or "2023: revised, 1 rows changed" not in text:
            problems.append(f"revision not stored: {text[-150:]}")
        if tl.editions(e.cur, 2023) != [(1, None, 2 * N), (2, 1, 2 * N)] or \
                tl.live(e.cur, 2023) != before[2023] or \
                m.status(e.cur, ZZ)["pending_refresh"] != ["2023"]:
            problems.append("the revision touched live before refresh-latest")
        rc, text, _, _ = e.run_main(["refresh-latest"])
        if rc != 0 or tl.live(e.cur, 2023) != before[2023]:
            problems.append("refresh-latest preview wrote")
        e.cur.execute(f"UPDATE public.{ZL} SET loaded_at = "
                      "'2020-01-01T00:00:00Z' WHERE reporting_year = 2023")
        rc, text, _, _ = e.run_main(["refresh-latest", "--commit"])
        now = tl.live(e.cur, 2023)
        if rc != 0 or now[CODES[1]] != before[2023][CODES[1]] + 3 or \
                {k: v for k, v in now.items() if k != CODES[1]} != \
                {k: v for k, v in before[2023].items() if k != CODES[1]}:
            problems.append("refresh-latest changed more than the revised "
                            "cell of 2023")
        if tl.live_sources(e.cur, 2023) != [m.guidance_url("2025")]:
            problems.append("live source of 2023 is not uniformly the new "
                            "edition's")
        for y in (2020, 2021, 2022, 2024):
            if tl.live(e.cur, y) != before[y] or \
                    tl.live_sources(e.cur, y) != src[y]:
                problems.append(f"{y} changed although it was not revised")
        e.cur.execute(f"SELECT DISTINCT loaded_at FROM public.{ZE} WHERE "
                      "reporting_year = 2023 AND edition = 2")
        (ed_at,), = e.cur.fetchall()
        e.cur.execute(f"SELECT MAX(loaded_at), (SELECT loaded_at FROM "
                      f"public.{ZL} WHERE reporting_year = 2023 AND lad24cd = "
                      "%s AND age_group = '17-21') FROM public." + ZL
                      + " WHERE reporting_year = 2023", (CODES[1],))
        if e.cur.fetchone() != (ed_at, ed_at):
            problems.append("live loaded_at of the revised row is not the "
                            "edition's")
        if not m.status(e.cur, ZZ)["ok"]:
            problems.append("status not clean after refresh-latest")
    scenario(cur, body)
    report(15, name, not problems,
           "only 2023's revised cell and its source and loaded_at moved; "
           "2020-2022 and 2024 unchanged" if not problems else
           "; ".join(problems[:3]))


# --------------------------------------------------------------- gate 16

def gate_16_key_changes(cur):
    name = ("key changes (authorities added or removed) only when each year "
            "is named in --accept-key-changes")
    problems = []

    def body(e):
        extra = "E10000007"
        years = range(2021, 2026)
        e.release("2025", years, (2023, 2024, 2025),
                  codes17=CODES + [extra], codes22=CODES,
                  allz17=[(extra, y) for y in years if y != 2023])
        e.latest = "2025"
        with mock.patch.object(m, "NATIONAL_CHANGE", 0.5):
            rc, text, _, _ = e.run_main(["load", "--commit"])
        if rc != 0:
            problems.append(f"setup load: {text[-150:]}")
            return
        live0 = tl.live(e.cur, 2023)
        rc, text, _, _ = e.run_main(["refresh-latest"])
        if rc != 0 or f"2023: keys added 1 ({extra}/17-21)" not in text:
            problems.append("preview does not list the added key")
        before = state(e.cur)
        ok, text = halts(e, ["refresh-latest", "--commit"], before)
        if not (ok and "not named" in text):
            problems.append("refresh-latest --commit without naming the "
                            "year did not halt")
        ok, text = halts(e, ["refresh-latest", "--commit",
                             "--accept-key-changes", "2024"], before)
        if not ok:
            problems.append("naming another year did not halt")
        rc, text, _, _ = e.run_main(["refresh-latest", "--commit",
                                     "--accept-key-changes", "2023"])
        now = tl.live(e.cur, 2023)
        if rc != 0 or extra not in now or extra in live0 or \
                {k: v for k, v in now.items() if k != extra} != live0:
            problems.append("naming 2023 did not add exactly the key")
        if tl.live_sources(e.cur, 2023) != [m.guidance_url("2025")] or \
                not m.status(e.cur, ZZ)["ok"]:
            problems.append("source or status after the key change")
    scenario(cur, body)
    report(16, name, not problems,
           "the preview lists the key; refresh-latest halts unnamed; naming "
           "the year adds exactly that key" if not problems else
           "; ".join(problems[:3]))


# --------------------------------------------------------------- gate 17

EDITION1_HASH_SQL = """
SELECT COUNT(*), md5(string_agg(jsonb_build_object({pairs})::text,
       E'\\n' ORDER BY lad24cd, reporting_year, age_group))
FROM (SELECT e.*, (SELECT s FROM unnest(string_to_array(regexp_replace(
         e.source_file, '^as loaded: (.*) \\(loaded [^)]*\\)$', '\\1'),
         ' | ')) s WHERE s LIKE '% age group ' || e.age_group LIMIT 1)
         AS src
      FROM public.{table} e WHERE e.edition = 1) t
"""


def edition1_hash(cur, spec=SPEC):
    """(rows, md5) of every year's edition 1, computed as m.live_state does
    for the live table: each row as jsonb of the live columns (the value and
    attribution columns, source recovered from the as-loaded source_file),
    without loaded_at and null_reasons, ordered by key."""
    cols = ("lad24cd", "reporting_year", "age_group") + tuple(
        c for c in m.DATA_COLUMNS if c != "null_reasons")
    pairs = ", ".join(f"'{c}', {c}" for c in cols) + ", 'source', src"
    cur.execute(EDITION1_HASH_SQL.format(pairs=pairs,
                                         table=spec.editions_table))
    return cur.fetchone()


def real_edition1_hash(cur, spec=SPEC, note=NOTE, rows=None, md5=None):
    rows = m.LIVE_ROWS if rows is None else rows
    md5 = m.LIVE_HASH if md5 is None else md5
    n, h = edition1_hash(cur, spec)
    bad = []
    if (n, h) != (rows, md5):
        bad.append(f"edition 1 rows/hash {n}/{h}, expected {rows}/{md5}")
    if not Path(note).exists():
        bad.append(f"decision note {Path(note).name} not written")
    else:
        mt = re.search(r"before-state (?:row )?hash[^0-9a-f]{0,40}([0-9a-f]"
                       r"{32})", Path(note).read_text(encoding="utf-8"),
                       re.I)
        if not mt:
            bad.append("the decision note records no before-state hash")
        elif mt.group(1) != h:
            bad.append(f"edition 1 hash {h} differs from the note's "
                       f"{mt.group(1)}")
    return not bad, "; ".join(bad) if bad else (
        f"edition 1 of every year ({n} rows) hashes to {h}, the before-state "
        "hash in the decision note")


def gate_17_before_state_hash(cur):
    name = ("edition 1 of every year equals the before-state hash recorded "
            "in the decision note")
    out = {}

    def body(e, files):
        n, h = m.live_state(e.cur, ZZ)
        rc, text, _, _ = e.migrate_cmd(files)
        if rc != 0:
            out["error"] = text[-200:]
            return
        out["same"] = edition1_hash(e.cur, ZZ) == (n, h)
        # live changed after migration no longer matches (the check can fail)
        e.cur.execute(f"UPDATE public.{ZL} SET foyers = COALESCE(foyers, 0) "
                      "+ 1 WHERE lad24cd = %s AND reporting_year = 2024 AND "
                      "age_group = '17-21'", (CODES[1],))
        out["changed"] = m.live_state(e.cur, ZZ) != (n, h)
        rc, text, _, _ = e.migrate_cmd(files)
        out["twice"] = rc == "halt" and ("runs once" in text
                                         or "not as surveyed" in text)
    try:
        legacy_world(cur, body)
    except (psycopg2.Error, SystemExit, RuntimeError) as ex:
        return report(17, name, False, str(ex).splitlines()[0])
    ok = out.get("same") and out.get("changed") and out.get("twice")
    mixed(17, name, ok,
          "a seeded migration stores an edition 1 whose hash equals the "
          "before-state hash; a later change shows; a second migration is "
          "refused" if ok else f"seeded: {out}", cur, real_edition1_hash)


# --------------------------------------------------------------- gate 18

def real_correction_proof(cur, spec=SPEC, files=None, raw_dir=None,
                          cells_expected=None, rows25_expected=None):
    """The migration proof re-run read-only from the files on disk (found by
    sha256) against the stored editions: every non-NULL corrected 17-21 cell
    equals the cell of edition 1; every 22-25 row of edition 1 equals the
    file's age-25 row; the correction edition stored for each year equals
    what the files produce now."""
    files = m.LEGACY_FILES if files is None else files
    raw = Path(raw_dir) if raw_dir is not None else m.RAW_DIR
    by_sha = {m.content_sha256(p): p for p in sorted(raw.glob("*.csv"))}
    metas = {}
    for k, f in files.items():
        p = by_sha.get(f["sha256"])
        if p is None:
            return False, (f"{k}: no file in {raw} with sha256 "
                           f"{f['sha256'][:16]}")
        metas[k] = m.read_file(p)
    codes, by_year = set(), {}
    for k, meta in metas.items():
        codes |= {c for c, _ in meta["rows"]}
        for y, cs in m.codes_with_data(meta).items():
            if y in files[k]["years"]:
                by_year.setdefault((meta["cohort"], y), set()).update(cs)
    rmap, problems = m.resolve_codes(cur, codes, by_year)
    if problems:
        return False, "geography: " + "; ".join(problems[:2])
    corr = {}
    for k, meta in metas.items():
        built = m.build_rows(meta, rmap)
        for y in files[k]["years"]:
            for r in built.get(y, []):
                corr[(r["lad24cd"], y, r["age_group"])] = r
    years = sorted({k[1] for k in corr})
    held, stored = {}, {}
    for y in years:
        for r in m.edition_records(cur, spec, y, 1):
            held[(r["lad24cd"], y, r["age_group"])] = r
        cur.execute(f"SELECT edition FROM public.{spec.editions_table} "
                    "WHERE reporting_year = %s AND release_label LIKE "
                    "'rule 1 correction%%' GROUP BY 1", (y,))
        eds = [r[0] for r in cur.fetchall()]
        if len(eds) != 1:
            return False, f"{y}: {len(eds)} correction editions"
        for r in m.edition_records(cur, spec, y, eds[0]):
            stored[(r["lad24cd"], y, r["age_group"])] = r
    bad, cells, rows25 = [], 0, 0
    for key in sorted(set(held) & set(corr)):
        if key[2] != "17-21":
            continue
        for c in m.PROOF_COLS_1721:
            if corr[key][c] is None:
                continue
            cells += 1  # not a source value
            if corr[key][c] != held[key][c]:
                bad.append(f"{key} {c}: held {held[key][c]}, file "
                           f"{corr[key][c]}")
    f22 = metas["f22_2025"]["rows"]
    back = {rmap[c]: c for c in {c for c, _ in f22}}
    for key in sorted(k for k in held if k[2] == "22-25"):
        cells25 = f22.get((back.get(key[0]), key[1]))
        if cells25 is None:
            bad.append(f"{key}: not in the file")
        elif all(held[key][c] == cells25[("25", cat)][0]
                 for c, cat in m.AGE25):
            rows25 += 1  # not a source value
        else:
            bad.append(f"{key}: not the file's age-25 row")
    if set(stored) != set(corr):
        bad.append(f"correction keys differ: {len(set(stored) - set(corr))} "
                   f"stored only, {len(set(corr) - set(stored))} files only")
    else:
        diff = [k for k in corr
                if any(stored[k][c] != corr[k][c] for c in m.DATA_COLUMNS)]
        if diff:
            bad.append(f"{len(diff)} correction rows differ from the files, "
                       f"e.g. {diff[:2]}")
    if cells_expected is not None and cells != cells_expected:
        bad.append(f"{cells} proof cells, expected {cells_expected}")
    if rows25_expected is not None and rows25 != rows25_expected:
        bad.append(f"{rows25} age-25 rows, expected {rows25_expected}")
    return not bad, "; ".join(bad[:3]) if bad else (
        f"{cells:,} non-NULL corrected 17-21 cells equal edition 1 (0 "
        f"differences); {rows25} edition 1 22-25 rows equal the file's age-25 "
        "row; the stored correction equals the files read now")


def real_proof(cur):
    return real_correction_proof(cur, SPEC, cells_expected=5289,
                                 rows25_expected=396)


def gate_18_correction_proof(cur):
    name = ("the correction proof re-run read-only from the files on disk; "
            "migrate-legacy proof passes, a planted difference or "
            "unexplained dropped key stops")
    problems = []

    def ok_world(e, files):
        rc, text, _, _ = e.migrate_cmd(files)
        if rc != 0 or "0 differences" not in text:
            problems.append(f"proof did not pass: {text[-150:]}")
            return
        ok, detail = real_correction_proof(
            e.cur, ZZ, files=e.fx, raw_dir=files[0].parent)
        if not ok:
            problems.append(f"re-run from disk: {detail}")
        # a file changed on disk is not found by sha
        files[0].write_text(files[0].read_text(encoding="utf-8") + "\n",
                            encoding="utf-8")
        ok, detail = real_correction_proof(
            e.cur, ZZ, files=e.fx, raw_dir=files[0].parent)
        if ok or "no file" not in detail:
            problems.append("a changed file on disk was accepted")

    def planted(sql, args, needle):
        def f(e, files):
            e.cur.execute(sql, args)
            before = state(e.cur)
            rc, text, _, logged = e.migrate_cmd(files)
            if not (rc == "halt" and needle in text
                    and state(e.cur) == before and not logged.called):
                problems.append(f"planted {needle!r}: rc {rc}: "
                                f"{text[-150:]}")
        return f
    legacy_world(cur, ok_world)
    legacy_world(cur, planted(
        f"UPDATE public.{ZL} SET independent_living = independent_living + 1"
        " WHERE lad24cd = %s AND reporting_year = 2024 AND age_group = "
        "'17-21'", (CODES[1],), "proof failed: 1 corrected 17-21 cells"))
    legacy_world(cur, planted(
        f"UPDATE public.{ZL} SET not_known = 7 WHERE lad24cd = %s AND "
        "reporting_year = 2024 AND age_group = '22-25'", (CODES[1],),
        "age-25 row"))
    legacy_world(cur, planted(
        f"INSERT INTO public.{ZL} (lad24cd, reporting_year, age_group, "
        "foyers) VALUES ('E06000005', 2022, '17-21', 4)", (),
        "does not explain"))
    mixed(18, name, not problems,
          "seeded migration proof passes (0 differences) and re-runs from "
          "the files by sha256; a planted held difference, a 22-25 row that "
          "is not the age-25 row and an unexplained dropped key each stop "
          "with nothing stored" if not problems else
          "; ".join(problems[:3]), cur, real_proof)


# --------------------------------------------------------------- gate 19

W1_JOIN = re.compile(
    r"LEFT JOIN care_leaver_accommodation cl\s+ON cl\.lad24cd = b\.lad24cd\s+"
    r"AND cl\.age_group = '17-21'\s+AND cl\.reporting_year = \(SELECT "
    r"MAX\(reporting_year\)\s+FROM care_leaver_accommodation\s+WHERE "
    r"age_group = '17-21'\)")


def w1_query(table):
    """W1's care leaver join of sql/w1/05_la_signals.sql, read from the file,
    with care_leaver_accommodation replaced by `table`."""
    text = (HERE.parent / "sql" / "w1" / "05_la_signals.sql").read_text(
        encoding="utf-8")
    mt = W1_JOIN.search(text)
    if not mt or "cl.semi_independent AS care_leavers_semi_indep" not in text:
        raise ValueError("the care leaver join was not found in "
                         "sql/w1/05_la_signals.sql")
    join = mt.group(0).replace("care_leaver_accommodation", f"public.{table}")
    return (f"SELECT b.lad24cd, cl.semi_independent FROM (SELECT DISTINCT "
            f"lad24cd FROM public.{table}) b {join}")


def w1_matches_tip(cur, live_table, editions_table, spec):
    cur.execute(w1_query(live_table))
    got = dict(cur.fetchall())
    cur.execute(f"SELECT MAX(reporting_year) FROM public.{live_table} WHERE "
                "age_group = '17-21'")
    (latest,) = cur.fetchone()
    if latest is None:
        return False, "no 17-21 rows", {}
    tip = core.chain_tip(cur, spec, str(latest))
    cur.execute(f"SELECT lad24cd, semi_independent FROM "
                f"public.{editions_table} WHERE reporting_year = %s AND "
                "edition = %s AND age_group = '17-21'", (latest, tip))
    want = dict(cur.fetchall())
    expect = {k: want.get(k) for k in got}
    diff = {k for k in set(got) | set(want)
            if got.get(k) != want.get(k) and not (k not in got)}
    return (got == expect and set(want) <= set(got) and bool(want),
            f"{latest} ed{tip}: {len(want)} authorities, {len(diff)} differ",
            want)


def real_w1(cur, spec=SPEC):
    ok, detail, want = w1_matches_tip(cur, spec.live_table,
                                      spec.editions_table, spec)
    nulls = sum(1 for v in want.values() if v is None)
    return ok, (f"W1's join on live returns the tip's semi_independent for "
                f"{detail} ({nulls} NULL from suppressed parts, as W1 "
                "expects)" if ok else detail)


def gate_19_w1(cur):
    name = ("W1's care leaver join on live returns the tip's 17-21 values "
            "for the latest year")
    out = {}

    def body(e):
        sup = {(CODES[2], 2024, Y17, "Foyers"): "c"}
        e.seed(values17=sup)
        out["ok"] = w1_matches_tip(e.cur, ZL, ZE, ZZ)
        e.cur.execute(f"UPDATE public.{ZL} SET semi_independent = "
                      "semi_independent + 1 WHERE lad24cd = %s AND "
                      "reporting_year = 2024 AND age_group = '17-21'",
                      (CODES[0],))
        out["drift"] = w1_matches_tip(e.cur, ZL, ZE, ZZ)
    try:
        scenario(cur, body, seed=False)
    except (psycopg2.Error, SystemExit, RuntimeError, ValueError) as ex:
        return report(19, name, False, str(ex).splitlines()[0])
    seeded = out["ok"][0] and not out["drift"][0]
    mixed(19, name, seeded,
          f"seeded latest year {out['ok'][1]}; a drifted live value is "
          "caught" if seeded else f"seeded: {out}", cur, real_w1)


# --------------------------------------------------------------- gates 20-22

def gate_20_rerun(cur):
    name = "rerun idempotent: a second load changes nothing"
    problems = []

    def body(e):
        e.release("2025", range(2021, 2026), (2023, 2024, 2025))
        e.latest = "2025"
        rc, text, _, _ = e.run_main(["load", "--commit"])
        snap = state(e.cur)
        rc2, text2, _, logged = e.run_main(["load", "--commit"])
        if rc != 0 or rc2 != 0 or state(e.cur) != snap or logged.called \
                or "nothing parsed" not in text2:
            problems.append(f"second load changed state or ran: {text2[-150:]}")
        rc3, text3, _, logged = e.run_main(["load", "--release", "2024",
                                            "--commit"])
        if state(e.cur) != snap and rc3 == 0:
            problems.append("loading the held 2024 release again changed "
                            "state")
        rc4, _, _, _ = e.run_main(["refresh-latest", "--commit"])
        if rc4 != 0 or state(e.cur)[:2] != snap[:2]:
            problems.append("refresh-latest with nothing pending changed "
                            "live or editions")
    scenario(cur, body)
    report(20, name, not problems, "a second load, a held-release load and "
           "refresh-latest with nothing pending stored nothing and logged "
           "nothing" if not problems else "; ".join(problems[:3]))


def gate_21_stranded(cur):
    name = "stranded year repair (editions held, live rows missing)"
    problems = []

    def body(e):
        e.cur.execute(f"DELETE FROM public.{ZL} WHERE reporting_year = 2022")
        if m.status(e.cur, ZZ)["live_missing"] != ["2022"]:
            problems.append("status does not name the stranded year")
        rc, text, _, _ = e.run_main(["load", "--release", "2024",
                                     "--commit"])
        led = tl.ledger(e.cur, 2022)
        if rc != 0 or "no new edition" not in text or \
                tl.editions(e.cur, 2022) != [(1, None, N)] or \
                tl.count(e.cur, ZL, "WHERE reporting_year = 2022") != N or \
                led[-1][1] != "live-missing" or not m.status(e.cur,
                                                             ZZ)["ok"]:
            problems.append(f"repair failed: {text[-200:]}")
    scenario(cur, body)
    report(21, name, not problems, "the stranded year was rebuilt from its "
           "edition 1 (no new edition) with a live-missing ledger row"
           if not problems else "; ".join(problems[:3]))


def gate_22_restore(cur):
    name = "restore-edition round trip"
    problems = []

    def body(e):
        e.release("2025", range(2021, 2026), (2023, 2024, 2025),
                  values17={(CODES[1], 2023, Y17, "Foyers"): "5"})
        e.latest = "2025"
        rc, text, _, _ = e.run_main(["load", "--commit"])
        rc, text, _, _ = e.run_main(["refresh-latest", "--commit"])
        before = tl.live(e.cur, 2023)
        snap = state(e.cur)
        rc, text, _, _ = e.run_main(["restore-edition", "2023", "1"])
        if rc != 0 or state(e.cur) != snap:
            problems.append("restore-edition preview wrote")
        rc, text, _, logged = e.run_main(["restore-edition", "2023", "1",
                                          "--commit"])
        e.cur.execute(f"SELECT DISTINCT release_label FROM public.{ZE} "
                      "WHERE reporting_year = 2023 AND edition = 3")
        label = e.cur.fetchall()
        if rc != 0 or tl.editions(e.cur, 2023) != [
                (1, None, 2 * N), (2, 1, 2 * N), (3, 2, 2 * N)] or \
                label != [("restored from edition 1",)] or \
                not logged.called:
            problems.append(f"restore --commit: {text[-150:]}")
        rc, text, _, _ = e.run_main(["refresh-latest", "--commit"])
        now = tl.live(e.cur, 2023)
        if rc != 0 or now[CODES[1]] != before[CODES[1]] - 3 or \
                core.rows_differing(e.cur, ZZ, "2023", 3):
            problems.append("refresh-latest did not apply the restored "
                            "edition")
        ed1, ed3 = (ed_rows(e.cur, 2023, 1), ed_rows(e.cur, 2023, 3))
        if ed1 != ed3:
            problems.append("edition 3 does not hold edition 1's rows")
        ok = []
        for argv in (["restore-edition", "2023", "3", "--commit"],
                     ["restore-edition", "2023", "9", "--commit"]):
            ok.append(e.run_main(argv)[0] == "halt")
        if not all(ok):
            problems.append("restoring the tip or a missing edition did "
                            "not halt")
        if not m.status(e.cur, ZZ)["ok"]:
            problems.append("status not clean after the round trip")
    scenario(cur, body)
    report(22, name, not problems, "edition 1 stored as edition 3 "
           "('restored from edition 1'), applied by refresh-latest; the tip "
           "and a missing edition are refused" if not problems else
           "; ".join(problems[:3]))


# --------------------------------------------------------------- gate 23

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


def gate_23_no_network_no_secrets(cur):
    name = ("no network, no secret in source or output; a download never "
            "overwrites a same-named file with different content")
    files = [Path(__file__).resolve(), HERE / "s4_care_leaver_editions.py",
             HERE / "period_editions.py", HERE / "geography.py"]
    attempts = []

    def refuse(*a, **k):
        attempts.append(a[:1])
        raise OSError("network blocked by the verify script")

    def body(c):
        with env(c) as e:
            with mock.patch.object(socket.socket, "connect", refuse), \
                    mock.patch.object(socket, "create_connection", refuse), \
                    mock.patch.object(socket, "getaddrinfo", refuse):
                e.seed()
                e.release("2025", range(2021, 2026), (2023, 2024, 2025))
                e.latest = "2025"
                rc, _, _, _ = e.run_main(["load", "--commit"])
                rc_s, _, _, _ = e.run_main(["status"])
                rc_r, _, _, _ = e.run_main(["refresh-latest"])
                dest = e.root / "raw" / "2025_1721_accommodation_aaaaaaaa.csv"
                url = m.csv_url("aaaaaaaa-0000")

                def body_of(v):
                    buf = io.StringIO()
                    csv.writer(buf).writerows(file_rows(
                        "care_leaver_2025", "17-21", ["E06000001"], [2025],
                        values={("E06000001", 2025, Y17, "Foyers"): v}))
                    return buf.getvalue().encode("utf-8")
                with contextlib.redirect_stdout(io.StringIO()), \
                        mock.patch.object(m, "MIN_FILE_BYTES", 100):
                    s1 = tl._Session(tl._Resp(content=body_of("2")))
                    first = m.fetch_csv(url, dest, s1)
                    sha1 = m.content_sha256(first)
                    s2 = tl._Session(tl._Resp(content=body_of("7")))
                    second = m.fetch_csv(url, dest, s2)
                kept = (first == dest and m.content_sha256(dest) == sha1
                        and second != dest
                        and second.name == f"{dest.stem}-"
                        f"{m.content_sha256(second)[:8]}.csv")
                calls = s1.calls + s2.calls
                ua = all("User-Agent" in kw.get("headers", {})
                         for _, kw in calls)
            return rc, rc_s, rc_r, kept, [u for u, _ in calls], ua
    try:
        rc, rc_s, rc_r, kept, calls, ua = _in_savepoint(cur, body)
    except Exception as ex:  # a blocked socket or any other failure
        return report(23, name, False, f"{type(ex).__name__}: {ex}")
    pat = re.compile(r"""(?i)(api[_-]?key|password|token|secret)['"]?\s*[:=]"""
                     r"""\s*['"][A-Za-z0-9]{16,}""")
    texts = {f.name: f.read_text(encoding="utf-8") for f in files}
    literal = [n for n, t in texts.items() if pat.search(t)]
    secrets = secret_values()
    in_src = sorted({k for k, v in secrets.items()
                     for t in texts.values() if v in t})
    in_out = sorted({k for k, v in secrets.items()
                     for line in OUTPUT if v in line})
    ok = (not attempts and rc == 0 and rc_s == 0 and rc_r == 0 and kept
          and calls and all(u.startswith("https://") for u in calls) and ua
          and not literal and not in_src and not in_out)
    report(23, name, ok, f"socket attempts {len(attempts)}; load, status and "
           f"refresh-latest through stubs returned {rc}/{rc_s}/{rc_r}; "
           f"{len(calls)} stubbed download request(s) over https with a "
           f"User-Agent={ua}; a same-named different file kept the first="
           f"{kept}; secret-like literal in source {literal or 'none'}; "
           f"{len(secrets)} secret-named settings checked against "
           f"{len(files)} files and {len(OUTPUT)} output lines: in source "
           f"{in_src or 'none'}, in output {in_out or 'none'}")


# --------------------------------------------------------------------- main

def gate_2_edition1_and_latest(cur):
    real_gate(cur, 2, "every live year has edition 1; the latest edition "
              "equals live cell for cell", real_edition1_and_latest)


def gate_3_source_uniform(cur):
    real_gate(cur, 3, "live source uniform per year and equal to the tip's",
              real_source_uniform)


GATES = (gate_1_table_shape_and_immutability, gate_2_edition1_and_latest,
         gate_3_source_uniform, gate_4_codes, gate_5_one_row,
         gate_6_null_vs_zero, gate_7_built_null, gate_8_sums_22_25,
         gate_9_identity, gate_10_older_release, gate_11_stop_conditions,
         gate_12_one_transaction, gate_13_ledger,
         gate_14_preview_writes_nothing, gate_15_revision_and_refresh,
         gate_16_key_changes, gate_17_before_state_hash,
         gate_18_correction_proof, gate_19_w1, gate_20_rerun,
         gate_21_stranded, gate_22_restore, gate_23_no_network_no_secrets)


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
            for gate in GATES:
                run_gate(cur, gate)
    finally:
        conn.rollback()
        conn.close()
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
