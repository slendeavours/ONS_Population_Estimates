"""RO4 edition history: append-only table of every published release of the RO4
housing expenditure data, by financial year.

ro4_housing_expenditure holds one row per authority per financial year (the
latest layer, read unchanged by the W1 nodes and the map export). This module
owns ro4_housing_expenditure_editions, which keeps every release held of a year
so a later release never overwrites what was sent. Rows are immutable: triggers
raise on UPDATE, DELETE and TRUNCATE. It mirrors s1b_editions.py and reuses the
generic helpers of s1_editions.py (latest_edition, classify_period, status,
period_hashes, guard_problems), passing period_col='financial_year'.

Edition model. Ordering is the `supersedes` chain, never published_date. The
chain can only be extended at its tip: a release published earlier than one
already held cannot be placed before it. Earlier releases of a year that were
never loaded are therefore not recorded, and not fetched.

Subcommands:
    python scripts/ro4_editions.py ddl        # idempotent
    python scripts/ro4_editions.py backfill [--commit | --simulate]
        # edition 1 of every financial year in the live table that has no
        # edition yet. If the manifest names a local file for the year, its sha256
        # matches, and re-parsing it with s2_ro4_load.parse gives exactly the live
        # rows, edition 1 records that file (manifest label and published date,
        # file sha256). Otherwise the live rows are recorded 'as loaded' (label
        # and date from the live row's own source text, canonical-rows sha256, no
        # source_file) and the difference is reported. DRY-RUN by default;
        # --commit inserts and runs the backfill gates in the same transaction;
        # --simulate does the same and rolls back. Idempotent.
    python scripts/ro4_editions.py status
        # what needs action (new year, newer edition not yet in live, live
        # changed outside the editions table, bad row counts); exit 1 if any
    python scripts/ro4_editions.py sync-new | load | refresh-latest
        # built in Task 2 (halt until then)

A later release goes through `load` with a manifest entry
(scripts/ro4_editions_manifest.json) and a file placed in data/reference/, then
`refresh-latest`; s2_ro4_load.py is the parser and first-load tool only.
"""
import argparse
import hashlib
import json
import re
import sys
import warnings
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from s1_editions import (format_status as _format_status, halt,  # noqa: E402
                         latest_edition as _s1_latest_edition, latest_map,
                         live_period_counts, modal_count, sha256_file)
from s1_editions import status as _status  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TABLE = "ro4_housing_expenditure_editions"
LIVE = "ro4_housing_expenditure"
PERIOD = "financial_year"
TRIGGER = "ro4_editions_immutable"
TRUNCATE_TRIGGER = "ro4_editions_no_truncate"
MEASURES = ("nightly_paid_ta_gross_exp_000", "nightly_paid_ta_net_exp_000",
            "hostels_gross_exp_000", "hostels_net_exp_000", "bb_gross_exp_000",
            "bb_net_exp_000", "hra_admin_prevention_relief_net_exp_000",
            "total_homelessness_gross_exp_000",
            "total_homelessness_net_exp_000", "total_housing_gross_exp_000",
            "total_housing_net_exp_000")
# what refresh-latest may overwrite in the live table; never loaded_at
REFRESH_COLS = MEASURES + ("la_name", "data_missing", "source")
DATA_COLS = ("lad24cd", PERIOD) + REFRESH_COLS
KEY = ("lad24cd",)
HASH_KEY = (PERIOD, "lad24cd")
EXTRA_COLS = ("release_label", "published_date", "source_file",
              "source_sha256", "supersedes")

REPO = Path(__file__).resolve().parent.parent
REF_DIR = REPO / "data" / "reference"
MANIFEST = Path(__file__).resolve().parent / "ro4_editions_manifest.json"
TOLERANCE = Decimal("0.005")  # the numeric(12,2) cell, as s2_ro4_load.reproduce


def load_manifest() -> list:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def manifest_entry(fy: str) -> "dict | None":
    hits = [e for e in load_manifest() if e[PERIOD] == fy]
    if len(hits) > 1:
        halt(f"{len(hits)} manifest entries for {fy}; Task 2 selects by release")
    return hits[0] if hits else None


# ------------------------------------------------------------------ schema

def create_schema(cur) -> None:
    """Create the table and immutability triggers if absent. Idempotent."""
    t = TABLE
    measure_cols = ",\n        ".join(f"{c} numeric(12,2)" for c in MEASURES)
    cur.execute(f"""
    CREATE TABLE IF NOT EXISTS public.{t} (
        lad24cd        varchar(9) NOT NULL
            REFERENCES public.la_boundaries (lad24cd),
        {PERIOD}       varchar(7) NOT NULL
            CONSTRAINT {t}_{PERIOD}_chk CHECK ({PERIOD} ~ '^\\d{{4}}-\\d{{2}}$'),
        edition        integer    NOT NULL,
        la_name        varchar,
        {measure_cols},
        data_missing   boolean,
        source         text,
        release_label  text,
        published_date date,
        source_file    text,
        source_sha256  text,
        supersedes     integer,
        loaded_at      timestamptz DEFAULT now(),
        PRIMARY KEY (lad24cd, {PERIOD}, edition)
    )""")
    cur.execute(f"""
    CREATE OR REPLACE FUNCTION public.{TRIGGER}() RETURNS trigger AS $f$
    BEGIN
        RAISE EXCEPTION '{TABLE} is append-only: % is not permitted', TG_OP;
    END
    $f$ LANGUAGE plpgsql""")
    cur.execute(f"""CREATE OR REPLACE TRIGGER {TRIGGER}
        BEFORE UPDATE OR DELETE ON public.{TABLE}
        FOR EACH ROW EXECUTE FUNCTION public.{TRIGGER}()""")
    cur.execute(f"""CREATE OR REPLACE TRIGGER {TRUNCATE_TRIGGER}
        BEFORE TRUNCATE ON public.{TABLE}
        FOR EACH STATEMENT EXECUTE FUNCTION public.{TRIGGER}()""")


def table_exists(cur) -> bool:
    cur.execute("SELECT to_regclass(%s)", (f"public.{TABLE}",))
    return cur.fetchone()[0] is not None


def insert_edition(cur, recs: list, fy: str, *, release_label: str,
                   published_date: "date | None", source_file: "str | None",
                   source_sha256: str, supersedes: "int | None") -> int:
    """Insert one edition of a financial year; return its edition number.

    If source_sha256 is already recorded for the year nothing is inserted and
    the existing edition number is returned. Missing measures are stored NULL
    (never 0). Empty recs is a hard stop, and so is a `supersedes` that is not
    an existing edition of the year or a row of another year."""
    from psycopg2.extras import execute_values
    if not recs:
        halt(f"insert_edition: no records supplied for {fy}")
    cur.execute(f"SELECT DISTINCT edition FROM public.{TABLE} "
                f"WHERE {PERIOD} = %s AND source_sha256 = %s",
                (fy, source_sha256))
    existing = [r[0] for r in cur.fetchall()]
    if existing:
        return existing[0]
    cur.execute(f"SELECT COALESCE(MAX(edition), 0) FROM public.{TABLE} "
                f"WHERE {PERIOD} = %s", (fy,))
    edition = cur.fetchone()[0] + 1
    if supersedes is not None:
        cur.execute(f"SELECT 1 FROM public.{TABLE} WHERE {PERIOD} = %s "
                    "AND edition = %s LIMIT 1", (fy, supersedes))
        if cur.fetchone() is None:
            halt(f"insert_edition: supersedes={supersedes} is not an existing "
                 f"edition of {fy}")
    if any(r[PERIOD] != fy for r in recs):
        halt(f"insert_edition: records contain a financial year other than {fy}")
    cols = list(DATA_COLS) + ["edition"] + list(EXTRA_COLS)
    data = [[r.get(c) for c in DATA_COLS] + [edition, release_label,
            published_date, source_file, source_sha256, supersedes]
            for r in recs]
    execute_values(cur, f"INSERT INTO public.{TABLE} ({', '.join(cols)}) "
                   "VALUES %s", data, page_size=1000)
    return edition


def latest_edition(cur, fy: str) -> int:
    """Chain tip of the financial year; the validation (single root, no fork,
    no self/dangling supersedes, whole chain reachable) lives in
    s1_editions.latest_edition."""
    return _s1_latest_edition(cur, fy, table=TABLE, period_col=PERIOD)


# ----------------------------------------------------------------- derived

def expected_authorities(cur) -> "int | None":
    """Authorities per financial year, derived: the most common per-year row
    count of the live table (None if it is empty)."""
    return modal_count(live_period_counts(cur, LIVE, PERIOD))


def rows_sha256(recs: list) -> str:
    """sha256 of a canonical text rendering of one year's rows: one line per
    authority sorted by lad24cd, the columns after lad24cd in REFRESH_COLS order
    joined by '|', NULL as the empty string, booleans as true/false, lines joined
    by LF, UTF-8."""
    def cell(v):
        if v is None:
            return ""
        if isinstance(v, bool):
            return "true" if v else "false"
        return str(v)
    lines = ["|".join([r["lad24cd"]] + [cell(r.get(c)) for c in REFRESH_COLS])
             for r in sorted(recs, key=lambda r: r["lad24cd"])]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def live_recs(cur, fy) -> tuple:
    """(records, load dates, source texts) of one live financial year."""
    cols = ", ".join(DATA_COLS)
    cur.execute(f"""SELECT {cols}, (loaded_at AT TIME ZONE 'UTC')::date
                    FROM public.{LIVE} WHERE {PERIOD} = %s""", (fy,))
    rows = cur.fetchall()
    recs = [dict(zip(DATA_COLS, r[:-1])) for r in rows]
    return recs, {r[-1] for r in rows}, {r["source"] for r in recs}


def edition_rows(cur, fy, edition) -> dict:
    cols = ", ".join(DATA_COLS)
    cur.execute(f"SELECT {cols} FROM public.{TABLE} WHERE {PERIOD} = %s "
                "AND edition = %s", (fy, edition))
    return {r[0]: dict(zip(DATA_COLS, r)) for r in cur.fetchall()}


# ------------------------------------------------------------------ checks

def check_coverage(cur) -> tuple:
    """Every live financial year has edition 1; every edition (of any year in
    the table) has the derived authority count in distinct authorities and in
    rows. -> (problems, rows in edition 1, rows in table)."""
    cur.execute(f"SELECT DISTINCT {PERIOD} FROM public.{LIVE}")
    years = sorted(r[0] for r in cur.fetchall())
    cur.execute(f"""SELECT {PERIOD}, edition, COUNT(DISTINCT lad24cd), COUNT(*)
                    FROM public.{TABLE} GROUP BY 1, 2""")
    eds = {}
    for fy, e, n, rows in cur.fetchall():
        eds.setdefault(fy, {})[e] = (n, rows)
    want = expected_authorities(cur)
    bad, n1 = [], 0
    for fy in years:
        if 1 not in eds.get(fy, {}):
            bad.append(f"{fy}: no edition 1")
    for fy, e in sorted(eds.items()):
        for k, v in sorted(e.items()):
            n1 += v[1] if k == 1 else 0
            if v != (want, want):
                bad.append(f"{fy} ed{k}: {v[0]} authorities/{v[1]} rows, "
                           f"expected {want}/{want}")
    cur.execute(f"SELECT COUNT(*) FROM public.{TABLE}")
    return bad, n1, cur.fetchone()[0]


def _except_counts(cur, a_sql, a_args, b_sql, b_args) -> tuple:
    """(rows in A not in B, rows in B not in A); EXCEPT is NULL-safe."""
    out = []
    for x, xa, y, ya in ((a_sql, a_args, b_sql, b_args),
                         (b_sql, b_args, a_sql, a_args)):
        cur.execute(f"SELECT COUNT(*) FROM (({x}) EXCEPT ({y})) q", xa + ya)
        out.append(cur.fetchone()[0])
    return tuple(out)


def check_latest_equals_live(cur, years=None) -> list:
    """For each live financial year the latest edition equals the live rows on
    every refresh column, NULL-safe (0 is not NULL)."""
    cur.execute(f"SELECT DISTINCT {PERIOD} FROM public.{LIVE}")
    live_years = sorted(r[0] for r in cur.fetchall())
    cols = ", ".join(KEY + REFRESH_COLS)
    bad = []
    for fy in (years or live_years):
        try:
            ed = latest_edition(cur, fy)
        except (LookupError, ValueError) as e:
            bad.append(f"{fy}: {e}")
            continue
        ed_only, live_only = _except_counts(
            cur, f"SELECT {cols} FROM public.{TABLE} WHERE {PERIOD} = %s "
            "AND edition = %s", (fy, ed),
            f"SELECT {cols} FROM public.{LIVE} WHERE {PERIOD} = %s", (fy,))
        if ed_only or live_only:
            bad.append(f"{fy} ed{ed}: {ed_only} edition-only, {live_only} "
                       "live-only rows")
    return bad


def check_null_not_zero(cur) -> list:
    """In every stored edition data_missing is true exactly where the total
    homelessness gross figure is NULL (a real zero is kept as 0, a
    not-yet-reported authority is NULL)."""
    cur.execute(f"""SELECT {PERIOD}, edition,
        COUNT(*) FILTER (WHERE data_missing IS DISTINCT FROM
                         (total_homelessness_gross_exp_000 IS NULL))
        FROM public.{TABLE} GROUP BY 1, 2 ORDER BY 1, 2""")
    return [f"{fy} ed{e}: {n} rows where data_missing disagrees with a NULL "
            "total homelessness gross" for fy, e, n in cur.fetchall() if n]


def check_provenance(cur) -> tuple:
    """Edition 1 of each financial year: one label, date, file and sha256 for
    the whole edition; the sha is either the manifest file's (and then label,
    date and file equal the manifest's) or the canonical rows hash of the
    stored rows. -> (problems, info lines)."""
    cur.execute(f"""SELECT {PERIOD}, COUNT(DISTINCT release_label),
                    COUNT(DISTINCT published_date), COUNT(DISTINCT source_sha256),
                    COUNT(DISTINCT source_file), MIN(release_label),
                    MIN(published_date), MIN(source_file), MIN(source_sha256)
                    FROM public.{TABLE} WHERE edition = 1 GROUP BY 1 ORDER BY 1""")
    bad, info = [], []
    for fy, nl, nd, ns, nf, label, pub, src, sha in cur.fetchall():
        if (nl, nd, ns, nf) != (1, 1, 1, 1) and not (nl == nd == ns == 1
                                                     and nf == 0):
            bad.append(f"{fy} ed1: {nl} labels, {nd} dates, {ns} shas, "
                       f"{nf} files")
            continue
        entry = manifest_entry(fy)
        if entry is not None and sha == entry["sha256"]:
            kind = "the manifest file"
            if (label, pub.isoformat() if pub else None, src) != (
                    entry["release_label"], entry["published_date"],
                    entry["file"]):
                bad.append(f"{fy} ed1: records the manifest file's sha but its "
                           "label/date/file differ from the manifest")
        else:
            kind = "as loaded (canonical rows hash)"
            rows = list(edition_rows(cur, fy, 1).values())
            if sha != rows_sha256(rows):
                bad.append(f"{fy} ed1: sha256 is neither the manifest file's "
                           "nor the canonical hash of the stored rows")
        info.append(f"{fy} ed1 = {kind}, {pub}, {label!r}")
    for e in load_manifest():
        p = REF_DIR / e["file"]
        if p.exists() and sha256_file(p) != e["sha256"]:
            bad.append(f"manifest {e['file']}: local file sha256 differs from "
                       "the manifest")
    return bad, info


# ------------------------------------------------------------ file re-parse

def parse_file(cur, fy):
    """The independent parse of the year's local publisher file
    (s2_ro4_load.parse, header-driven); a DataFrame indexed by lad24cd."""
    import s2_ro4_load
    warnings.filterwarnings("ignore")
    df, *_ = s2_ro4_load.parse(fy, cur.connection)
    return df.set_index("lad24cd")


def _same_num(a, b) -> bool:
    if a is None or a != a:
        return b is None or b != b
    if b is None or b != b:
        return False
    return abs(Decimal(str(a)) - Decimal(str(b))) <= TOLERANCE


def compare_parsed(df, rows: dict) -> dict:
    """Parsed file (DataFrame by lad24cd) against stored rows ({lad: row}):
    authorities differing per column, plus names, data_missing and authorities
    in one side only. The 'source' text is not a value of the file."""
    only_file, only_rows = sorted(set(df.index) - set(rows)), sorted(set(rows) - set(df.index))
    per = {c: 0 for c in MEASURES}
    names = flags = 0
    for lad in sorted(set(df.index) & set(rows)):
        r, d = rows[lad], df.loc[lad]
        for c in MEASURES:
            if not _same_num(d[c], r[c]):
                per[c] += 1
        names += str(d["la_name"]) != str(r["la_name"])
        flags += bool(d["data_missing"]) != bool(r["data_missing"])
    equal = not (only_file or only_rows or names or flags or any(per.values()))
    return {"equal": equal, "per_column": per, "names": names,
            "data_missing": flags, "only_in_file": only_file,
            "only_in_rows": only_rows}


def describe(cmp: dict) -> str:
    if cmp["equal"]:
        return "equal"
    cols = ", ".join(f"{c}={n}" for c, n in cmp["per_column"].items() if n)
    return (f"differs: {cols or 'no measure'}; la_name={cmp['names']}, "
            f"data_missing={cmp['data_missing']}, only in file "
            f"{len(cmp['only_in_file'])}, only in rows {len(cmp['only_in_rows'])}")


def reparse_report(cur) -> list:
    """Re-parse every manifest file that is held locally and compare it with
    the edition 1 of its year, independent of the live table. Each result has
    fy, file, comparison, summary and problem ('' if the comparison is
    consistent with how edition 1 was recorded: the file's sha when it equals
    the rows, 'as loaded' when it does not)."""
    out = []
    for e in load_manifest():
        fy, path = e[PERIOD], REF_DIR / e["file"]
        if not path.exists():
            out.append({"fy": fy, "file": e["file"], "comparison": None,
                        "summary": f"{fy}: {e['file']} not held locally",
                        "problem": ""})
            continue
        cmp = compare_parsed(parse_file(cur, fy), edition_rows(cur, fy, 1))
        cur.execute(f"""SELECT DISTINCT source_sha256 FROM public.{TABLE}
                        WHERE {PERIOD} = %s AND edition = 1""", (fy,))
        shas = {r[0] for r in cur.fetchall()}
        recorded_file = shas == {e["sha256"]}
        problem = ""
        if recorded_file and not cmp["equal"]:
            problem = (f"{fy}: edition 1 records the file but the re-parse "
                       f"{describe(cmp)}")
        if not recorded_file and cmp["equal"]:
            problem = (f"{fy}: the re-parse equals edition 1 but edition 1 was "
                       "recorded as loaded, not as the file")
        out.append({"fy": fy, "file": e["file"], "comparison": cmp,
                    "summary": f"{fy} {e['file']} vs edition 1: {describe(cmp)} "
                               f"(edition 1 recorded as "
                               f"{'the file' if recorded_file else 'loaded'})",
                    "problem": problem})
    return out


# ---------------------------------------------------------------- backfill

PUBLISHED = re.compile(r"published (\d{1,2} [A-Za-z]+ \d{4})")


def _published_from_source(source, fallback):
    m = PUBLISHED.search(source or "")
    if m:
        try:
            return datetime.strptime(m.group(1), "%d %b %Y").date()
        except ValueError:
            try:
                return datetime.strptime(m.group(1), "%d %B %Y").date()
            except ValueError:
                pass
    return fallback


def backfill(cur, plan_only=False) -> list:
    """Edition 1 of every live financial year that has no edition. Years that
    already have one are skipped (live may since have been refreshed, and
    re-reading it would record a spurious 'as loaded' edition). Returns
    [(financial_year, edition, rows, action, how)]."""
    cur.execute(f"SELECT DISTINCT {PERIOD} FROM public.{LIVE} ORDER BY 1")
    years = [r[0] for r in cur.fetchall()]
    want = expected_authorities(cur)
    have_table = table_exists(cur)
    out = []
    for fy in years:
        if have_table:
            cur.execute(f"SELECT COUNT(*) FROM public.{TABLE} WHERE {PERIOD} = %s",
                        (fy,))
            n = cur.fetchone()[0]
            if n:
                out.append((fy, 1, n, "present", ""))
                continue
        recs, loaded, sources = live_recs(cur, fy)
        if len(recs) != want or len({r["lad24cd"] for r in recs}) != want:
            halt(f"{fy}: {len(recs)} live rows, expected {want} (the derived "
                 "count)")
        if len(loaded) != 1 or len(sources) != 1:
            halt(f"{fy}: live rows differ in loaded_at/source "
                 f"({len(loaded)} dates, {len(sources)} sources)")
        entry = manifest_entry(fy)
        path = REF_DIR / entry["file"] if entry else None
        cmp = None
        if path is not None and path.exists():
            if sha256_file(path) != entry["sha256"]:
                halt(f"{entry['file']}: sha256 does not match the manifest")
            cmp = compare_parsed(parse_file(cur, fy),
                                 {r["lad24cd"]: r for r in recs})
        if cmp is not None and cmp["equal"]:
            kw = dict(release_label=entry["release_label"],
                      published_date=date.fromisoformat(entry["published_date"]),
                      source_file=entry["file"], source_sha256=entry["sha256"])
            how = f"the file {entry['file']} (re-parse equals the live rows)"
        else:
            src = sources.pop()
            kw = dict(release_label=f"as loaded; {src}",
                      published_date=_published_from_source(src, loaded.pop()),
                      source_file=None, source_sha256=rows_sha256(recs))
            how = ("as loaded (canonical rows hash)"
                   + ("; local file not held" if cmp is None else
                      f"; local file {entry['file']} {describe(cmp)}"))
        if plan_only:
            out.append((fy, 1, len(recs), "would insert", how))
            continue
        ed = insert_edition(cur, recs, fy, supersedes=None, **kw)
        cur.execute(f"SELECT COUNT(*) FROM public.{TABLE} WHERE {PERIOD} = %s "
                    "AND edition = %s", (fy, ed))
        out.append((fy, ed, cur.fetchone()[0], "inserted", how))
    return out


def run_backfill_gates(cur) -> None:
    """Coverage, edition 1 equals live (NULL-safe), NULL-not-0, provenance;
    halt on any problem."""
    bad, _, _ = check_coverage(cur)
    bad += check_latest_equals_live(cur)
    bad += check_null_not_zero(cur)
    bad += check_provenance(cur)[0]
    if bad:
        halt("backfill failed its gates, rolled back: " + "; ".join(bad[:6]))


# ------------------------------------------------------------------ status

def status(cur) -> dict:
    """What needs action between the live table and its editions (read-only);
    the shape is s1_editions.status, keyed by financial year."""
    return _status(cur, LIVE, TABLE, KEY, REFRESH_COLS, period_col=PERIOD)


def format_status(st: dict) -> str:
    return _format_status(st, "RO4 housing expenditure").replace(
        "periods in the live table", "financial years in the live table")


# --------------------------------------------------------- Task 2 (stubs)

def sync_new(cur, expected_authorities_n=None):
    halt("sync-new: built in Task 2")


# -------------------------------------------------------------------- CLI

def cmd_ddl(_args):
    from _db import get_conn
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            create_schema(cur)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(f"ddl: {TABLE} and triggers {TRIGGER}, {TRUNCATE_TRIGGER} present")


def cmd_backfill(args):
    from _db import get_conn, get_readonly_conn
    writing = args.commit or args.simulate
    conn = get_conn() if writing else get_readonly_conn()
    try:
        with conn.cursor() as cur:
            if writing:
                create_schema(cur)
            results = backfill(cur, plan_only=not writing)
            if writing:
                run_backfill_gates(cur)
                cur.execute(f"SELECT COUNT(*) FROM public.{TABLE}")
                total = cur.fetchone()[0]
        for fy, ed, n, action, how in results:
            print(f"  {fy} edition {ed}: {action} ({n} rows)"
                  + (f" - {how}" if how else ""))
        added = sum(r[3] in ("inserted", "would insert") for r in results)
        if not writing:
            print(f"DRY RUN: {added} edition(s) would be added; nothing "
                  "written (use --commit or --simulate)")
            return
        if args.commit:
            conn.commit()
            print(f"backfill: {added} edition(s) added; gates passed; "
                  f"{TABLE} now holds {total} rows. COMMITTED")
        else:
            conn.rollback()
            print(f"SIMULATION: {added} edition(s) added, gates passed, "
                  "ROLLED BACK (nothing persisted)")
    except BaseException:
        if writing:
            conn.rollback()
        raise
    finally:
        conn.close()


def cmd_status(_args):
    from _db import get_readonly_conn
    conn = get_readonly_conn()
    try:
        with conn.cursor() as cur:
            if not table_exists(cur):
                halt(f"{TABLE} does not exist; run ddl then backfill")
            st = status(cur)
    finally:
        conn.close()
    print(format_status(st))
    sys.exit(0 if st["ok"] else 1)


def _later(_args):
    halt("built in Task 2")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ddl", help="create the table and triggers if absent"
                   ).set_defaults(func=cmd_ddl)
    bf = sub.add_parser("backfill", help="record edition 1 of every live "
                        "financial year (dry-run by default)")
    mode = bf.add_mutually_exclusive_group()
    mode.add_argument("--commit", action="store_true",
                      help="insert the editions (append-only, irreversible)")
    mode.add_argument("--simulate", action="store_true",
                      help="run the full --commit path and roll back")
    bf.set_defaults(func=cmd_backfill)
    sub.add_parser("status", help="what needs action; exit 1 if anything"
                   ).set_defaults(func=cmd_status)
    for name in ("sync-new", "load", "refresh-latest"):
        sub.add_parser(name, help="built in Task 2").set_defaults(func=_later)
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
