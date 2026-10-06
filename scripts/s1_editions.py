"""S1 edition history: append-only table of every published edition of each quarter.

la_statutory_homelessness holds one row per LA per quarter (the latest layer).
This module owns la_statutory_homelessness_editions, which keeps every
published edition of a quarter so a revision never overwrites what was sent.
Rows are immutable: a trigger raises on UPDATE, DELETE and TRUNCATE.

Subcommands (refresh-latest is added in a later step):
    python scripts/s1_editions.py ddl       # idempotent; safe to run repeatedly
    python scripts/s1_editions.py backfill  # edition 1 of every stored quarter
                                            # plus the 2025Q2 revision; idempotent
    python scripts/s1_editions.py diff --period P --new N --old M
    python scripts/s1_editions.py load --period P --manifest-entry N
        [--supersedes E] [--published-date YYYY-MM-DD] [--commit]
        # DRY-RUN by default: extracts the manifest file, diffs it against the
        # latest stored edition, writes nothing. Only --commit inserts.
        # --manifest-entry is a 0-based index into s1_editions_manifest.json.
    python scripts/s1_editions.py dryrun-all --out report.md
        # both manifest candidates of 2023Q2-2024Q4 vs stored edition 1 and
        # vs each other, as markdown; writes no table

Helpers imported by later steps: sha256_file, create_schema, insert_edition,
latest_edition, diff_editions, diff_records.
"""
import argparse
import hashlib
import csv
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TABLE = "la_statutory_homelessness_editions"
TRIGGER = "s1_editions_immutable"
TRUNCATE_TRIGGER = "s1_editions_no_truncate"

MEASURES = ("total_assessments", "owed_duty", "prevention_duty", "relief_duty",
            "households_in_ta", "support_needs_total",
            "mental_health_suspect", "learning_disability_suspect",
            "drug_dependency_suspect", "alcohol_dependency_suspect",
            "rough_sleeping_history_suspect")


def halt(msg):
    sys.exit(f"HALT: {msg}")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def create_schema(cur) -> None:
    """Create the table and immutability triggers if absent. Idempotent."""
    measure_cols = ",\n        ".join(f"{m} integer" for m in MEASURES)
    cur.execute(f"""
    CREATE TABLE IF NOT EXISTS public.{TABLE} (
        lad24cd varchar(9) NOT NULL
            REFERENCES public.la_boundaries (lad24cd),
        period varchar(6) NOT NULL CHECK (period ~ '^\\d{{4}}Q[1-4]$'),
        edition integer NOT NULL,
        {measure_cols},
        release_label text,
        published_date date,
        source_url text,
        source_file text,
        source_sha256 text,
        loaded_at timestamptz DEFAULT now(),
        supersedes integer,
        PRIMARY KEY (lad24cd, period, edition)
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


def insert_edition(cur, recs: list, period: str, *, release_label: str,
                   published_date: "date | None", source_url: "str | None",
                   source_file: str, source_sha256: str,
                   supersedes: "int | None") -> int:
    """Insert one edition of a period; return its edition number.

    If source_sha256 is already recorded for the period nothing is inserted and
    the existing edition number is returned. Missing measures are stored NULL.
    Empty recs is a hard stop: an edition with no rows cannot be recorded, and
    so is a `supersedes` that is not an existing edition of the period.
    """
    if not recs:
        halt(f"insert_edition: no records supplied for {period}")
    cur.execute(f"SELECT DISTINCT edition FROM public.{TABLE} "
                "WHERE period = %s AND source_sha256 = %s",
                (period, source_sha256))
    existing = [r[0] for r in cur.fetchall()]
    if existing:
        return existing[0]
    cur.execute(f"SELECT COALESCE(MAX(edition), 0) FROM public.{TABLE} "
                "WHERE period = %s", (period,))
    edition = cur.fetchone()[0] + 1
    if supersedes is not None:
        cur.execute(f"SELECT 1 FROM public.{TABLE} "
                    "WHERE period = %s AND edition = %s LIMIT 1",
                    (period, supersedes))
        if cur.fetchone() is None:
            halt(f"insert_edition: supersedes={supersedes} is not an existing "
                 f"edition of {period}")
    cols = (["lad24cd", "period", "edition"] + list(MEASURES) +
            ["release_label", "published_date", "source_url", "source_file",
             "source_sha256", "supersedes"])
    sql = (f"INSERT INTO public.{TABLE} ({', '.join(cols)}) "
           f"VALUES ({', '.join(['%s'] * len(cols))})")
    rows = []
    for r in recs:
        rows.append([r["lad24cd"], period, edition] +
                    [r.get(m) for m in MEASURES] +
                    [release_label, published_date, source_url, source_file,
                     source_sha256, supersedes])
    cur.executemany(sql, rows)
    return edition


def _values_differ(cur, period, ed_a, ed_b) -> bool:
    cols = ", ".join(("lad24cd",) + MEASURES)
    q = (f"SELECT COUNT(*) FROM (SELECT {cols} FROM public.{TABLE} "
         "WHERE period = %s AND edition = %s EXCEPT "
         f"SELECT {cols} FROM public.{TABLE} "
         "WHERE period = %s AND edition = %s) x")
    cur.execute(q, (period, ed_a, period, ed_b))
    a = cur.fetchone()[0]
    cur.execute(q, (period, ed_b, period, ed_a))
    return bool(a or cur.fetchone()[0])


def latest_edition(cur, period: str) -> int:
    """Edition with the highest published_date (ties: highest edition).

    Raises if two editions share the top published_date but differ in values,
    because the order is then unknowable and must not be guessed.
    """
    cur.execute(f"SELECT DISTINCT edition, published_date FROM public.{TABLE} "
                "WHERE period = %s", (period,))
    eds = cur.fetchall()
    if not eds:
        raise LookupError(f"no editions recorded for {period}")
    dated = [d for _, d in eds if d is not None]
    if dated and len(dated) != len(eds):
        undated = sorted(e for e, d in eds if d is None)
        raise ValueError(f"{period}: editions {undated} have no "
                         "published_date while others are dated; "
                         "order is ambiguous")
    if not dated:
        if len(eds) > 1:
            raise ValueError(f"{period}: several editions, none dated")
        return eds[0][0]
    top = max(dated)
    tied = sorted(e for e, d in eds if d == top)
    for other in tied[1:]:
        if _values_differ(cur, period, tied[0], other):
            raise ValueError(f"{period}: editions {tied} share published_date "
                             f"{top} and differ in values; order is ambiguous")
    return tied[-1]


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
    print(f"ddl: {TABLE} and trigger {TRIGGER} present")


REF_DIR = Path(__file__).resolve().parent.parent / "data" / "reference"
CSV_MEASURES = MEASURES[:6]
Q2_FILES = (  # (file, published, release_label, supersedes)
    ("statutory_homelessness_2025Q2.csv", date(2026, 2, 26), "Original", None),
    ("statutory_homelessness_2025Q2_revised.csv", date(2026, 4, 30),
     "Revised", 1),
)
LIVE_LABEL = "as loaded; published_date is the load date"


def live_text_sha256(recs: list) -> str:
    """sha256 of a canonical text rendering of one quarter's stored rows.

    Rendering: one line per authority, sorted by lad24cd, of the form
    lad24cd|m1|...|m11 over MEASURES in order, NULL as the empty string,
    lines joined by LF, no trailing newline, UTF-8.
    """
    lines = []
    for r in sorted(recs, key=lambda r: r["lad24cd"]):
        vals = ["" if r.get(m) is None else str(r[m]) for m in MEASURES]
        lines.append("|".join([r["lad24cd"]] + vals))
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _csv_recs(path: Path, mapping: dict, current: set) -> list:
    """Read one 2025Q2 CSV: '' -> NULL, publisher codes recoded to canonical."""
    recs = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            code = mapping.get(row["lad24cd"], row["lad24cd"])
            if code not in current:
                halt(f"{path.name}: {row['lad24cd']} is not a known lad24cd")
            rec = {"lad24cd": code}
            for m in CSV_MEASURES:
                v = (row.get(m) or "").strip()
                rec[m] = int(float(v)) if v != "" else None
            recs.append(rec)
    if len({r["lad24cd"] for r in recs}) != len(recs):
        halt(f"{path.name}: duplicate authorities after code resolution")
    return recs


def backfill(cur) -> list:
    """Edition 1 for the ten live quarters, editions 1-2 for 2025Q2.

    Live quarters are skipped when any edition already exists: the live table
    may since have been refreshed, and re-reading it would record a spurious
    'as loaded' edition. Returns a list of (period, edition, rows, inserted).
    """
    from s1_extract_ods import code_resolution
    current, mapping = code_resolution()
    out = []

    def run(period, recs, **kw):
        cur.execute(f"SELECT COUNT(*) FROM public.{TABLE} "
                    "WHERE period = %s", (period,))
        before = cur.fetchone()[0]
        ed = insert_edition(cur, recs, period, **kw)
        cur.execute(f"SELECT COUNT(*) FROM public.{TABLE} "
                    "WHERE period = %s AND edition = %s", (period, ed))
        n = cur.fetchone()[0]
        cur.execute(f"SELECT COUNT(*) FROM public.{TABLE} "
                    "WHERE period = %s", (period,))
        if n != 296:
            halt(f"{period} edition {ed}: {n} rows, expected 296")
        out.append((period, ed, n, cur.fetchone()[0] > before))

    cur.execute("SELECT DISTINCT period FROM public.la_statutory_homelessness "
                "ORDER BY 1")
    for (period,) in cur.fetchall():
        if period == "2025Q2":
            continue
        cur.execute(f"SELECT 1 FROM public.{TABLE} WHERE period = %s LIMIT 1",
                    (period,))
        if cur.fetchone():
            out.append((period, None, 0, False))
            continue
        cols = ", ".join(("lad24cd",) + MEASURES)
        cur.execute(f"""SELECT {cols}, (loaded_at AT TIME ZONE 'UTC')::date,
                               source_file
                        FROM public.la_statutory_homelessness
                        WHERE period = %s""", (period,))
        rows = cur.fetchall()
        recs = [dict(zip(("lad24cd",) + MEASURES, r[:-2])) for r in rows]
        loaded = {r[-2] for r in rows}
        files = {r[-1] for r in rows}
        if len(loaded) != 1 or len(files) != 1:
            halt(f"{period}: live rows differ in loaded_at/source_file "
                 f"({len(loaded)} dates, {len(files)} files)")
        run(period, recs, release_label=LIVE_LABEL,
            published_date=loaded.pop(), source_url=None,
            source_file=files.pop(), source_sha256=live_text_sha256(recs),
            supersedes=None)

    for fname, published, label, sup in Q2_FILES:
        path = REF_DIR / fname
        run("2025Q2", _csv_recs(path, mapping, current), release_label=label,
            published_date=published, source_url=None, source_file=fname,
            source_sha256=sha256_file(path), supersedes=sup)
    return out


def cmd_backfill(_args):
    from _db import get_conn
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            create_schema(cur)
            results = backfill(cur)
            cur.execute(f"SELECT COUNT(*) FROM public.{TABLE}")
            total = cur.fetchone()[0]
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    for period, ed, n, inserted in results:
        print(f"  {period} edition {ed}: "
              + ("inserted" if inserted else "already present"))
    print(f"backfill: {sum(r[3] for r in results)} edition(s) added; "
          f"{TABLE} now holds {total} rows")


MANIFEST = Path(__file__).resolve().parent / "s1_editions_manifest.json"
RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw" / "s1b_a3"
STALE_PERIODS = ("2023Q2", "2023Q3", "2023Q4", "2024Q1", "2024Q2", "2024Q3",
                 "2024Q4")


def _transition(old, new) -> str:
    if old == 0 and new is None:
        return "zero -> NULL"
    if old is None and new == 0:
        return "NULL -> zero"
    if old is None:
        return "NULL -> value"
    if new is None:
        return "value -> NULL"
    return "value -> value"


def _diff_maps(new_map: dict, old_map: dict) -> dict:
    """Core diff of two {lad24cd: {measure: value}} maps."""
    only_new = sorted(set(new_map) - set(old_map))
    only_old = sorted(set(old_map) - set(new_map))
    changed_auth = set()
    cells = {m: 0 for m in MEASURES}
    transitions = {m: Counter() for m in MEASURES}
    totals = Counter()
    for lad in set(new_map) & set(old_map):
        for m in MEASURES:
            o, n = old_map[lad].get(m), new_map[lad].get(m)
            if o != n:
                cells[m] += 1
                changed_auth.add(lad)
                t = _transition(o, n)
                transitions[m][t] += 1
                totals[t] += 1
    return {
        "authorities_changed": len(changed_auth),
        "cells_changed": cells,
        "no_change": not changed_auth and not only_new and not only_old,
        "transitions": {m: dict(c) for m, c in transitions.items() if c},
        "transition_totals": {t: totals.get(t, 0) for t in (
            "zero -> NULL", "NULL -> zero", "value -> value",
            "NULL -> value", "value -> NULL")},
        "only_in_new": only_new,
        "only_in_old": only_old,
    }


def _edition_map(cur, period, edition) -> dict:
    cols = ", ".join(("lad24cd",) + MEASURES)
    cur.execute(f"SELECT {cols} FROM public.{TABLE} "
                "WHERE period = %s AND edition = %s", (period, edition))
    return {r[0]: dict(zip(MEASURES, r[1:])) for r in cur.fetchall()}


def diff_editions(cur, period: str, new_edition: int, old_edition: int) -> dict:
    """Cell-level diff of two stored editions of one period (read-only).

    Returns authorities_changed, cells_changed {measure: n}, no_change, plus
    transitions per measure and transition_totals ('zero -> NULL',
    'NULL -> zero', 'value -> value', and the two NULL<->value kinds).
    """
    new_map = _edition_map(cur, period, new_edition)
    old_map = _edition_map(cur, period, old_edition)
    if not new_map or not old_map:
        halt(f"diff_editions: {period} edition "
             f"{new_edition if not new_map else old_edition} has no rows")
    return _diff_maps(new_map, old_map)


def diff_records(recs: list, cur, period: str, old_edition: int) -> dict:
    """As diff_editions, but the new side is extracted records not yet stored."""
    old_map = _edition_map(cur, period, old_edition)
    if not old_map:
        halt(f"diff_records: {period} edition {old_edition} has no rows")
    new_map = {r["lad24cd"]: {m: r.get(m) for m in MEASURES} for r in recs}
    return _diff_maps(new_map, old_map)


def load_manifest() -> list:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _extract_entry(entry: dict) -> tuple:
    """Extract one manifest file explicitly; verify its sha256. -> (recs, path)."""
    from s1_extract_ods import extract
    path = RAW_DIR / entry["file"]
    if not path.exists():
        halt(f"{path} not found")
    sha = sha256_file(path)
    if sha != entry["sha256"]:
        halt(f"{entry['file']}: sha256 {sha} does not match manifest "
             f"{entry['sha256']}")
    label = ("MHCLG Statutory Homelessness Detailed Local Authority Data, "
             + entry["file"])
    recs = extract(path, entry["period"], label)
    if len(recs) != 296 or len({r["lad24cd"] for r in recs}) != 296:
        halt(f"{entry['file']}: {len(recs)} authorities extracted, expected 296")
    return recs, path


def format_diff(d: dict) -> str:
    cells = ", ".join(f"{m}={n}" for m, n in d["cells_changed"].items() if n)
    t = ", ".join(f"{k}={v}" for k, v in d["transition_totals"].items() if v)
    return (f"authorities_changed={d['authorities_changed']}; "
            f"no_change={d['no_change']}; cells: {cells or 'none'}; "
            f"transitions: {t or 'none'}"
            + (f"; only_in_new={d['only_in_new']}" if d["only_in_new"] else "")
            + (f"; only_in_old={d['only_in_old']}" if d["only_in_old"] else ""))


def cmd_diff(args):
    from _db import get_readonly_conn
    conn = get_readonly_conn()
    try:
        with conn.cursor() as cur:
            d = diff_editions(cur, args.period, args.new, args.old)
    finally:
        conn.close()
    print(f"{args.period} edition {args.new} vs {args.old}: {format_diff(d)}")


def cmd_load(args):
    manifest = load_manifest()
    if not 0 <= args.manifest_entry < len(manifest):
        halt(f"--manifest-entry {args.manifest_entry} outside "
             f"0..{len(manifest) - 1}")
    entry = manifest[args.manifest_entry]
    if entry["period"] != args.period:
        halt(f"manifest entry {args.manifest_entry} is {entry['period']}, "
             f"not {args.period}")
    recs, path = _extract_entry(entry)
    published = (date.fromisoformat(args.published_date) if args.published_date
                 else date.fromisoformat(entry["last_modified"][:10]))
    from _db import get_conn, get_readonly_conn
    conn = get_conn() if args.commit else get_readonly_conn()
    try:
        with conn.cursor() as cur:
            try:
                prev = latest_edition(cur, args.period)
            except (LookupError, ValueError) as e:
                halt(f"cannot determine latest edition: {e}")
            supersedes = args.supersedes if args.supersedes is not None else prev
            d = diff_records(recs, cur, args.period, prev)
            print(f"{args.period} {entry['file']} (published {published}) "
                  f"vs stored edition {prev}:\n  {format_diff(d)}")
            if d["no_change"]:
                print("  NOTE: identical in values to the previous edition; "
                      "it would be recorded as a no_change edition, not a "
                      "revision")
            if not args.commit:
                print("DRY RUN: nothing written (use --commit to insert)")
                return
            ed = insert_edition(
                cur, recs, args.period, release_label=entry["release_label"],
                published_date=published, source_url=entry["url"],
                source_file=entry["file"], source_sha256=entry["sha256"],
                supersedes=supersedes)
            cur.execute(f"SELECT COUNT(*) FROM public.{TABLE} "
                        "WHERE period = %s AND edition = %s", (args.period, ed))
            n = cur.fetchone()[0]
            if n != 296:
                halt(f"{args.period} edition {ed}: {n} rows, expected 296")
        conn.commit()
        print(f"loaded {args.period} edition {ed} ({n} rows)")
    except BaseException:
        if args.commit:
            conn.rollback()
        raise
    finally:
        conn.close()


def _s1b_needs(cur, period) -> dict:
    """lad24cd -> S1b 'households with one or more support needs' (A3)."""
    cur.execute("SELECT lad24cd, value FROM public.la_homelessness_support_needs "
                "WHERE period = %s AND "
                "category_code = 'hh_one_or_more_support_needs'", (period,))
    return dict(cur.fetchall())


def _needs_match(vals: dict, s1b: dict) -> tuple:
    """(equal, differ) authority counts; NULL equals NULL."""
    eq = sum(1 for lad, v in vals.items() if s1b.get(lad) == v)
    return eq, len(vals) - eq


def _fmt_cells(d):
    return ", ".join(f"{k}={v}" for k, v in d["cells_changed"].items() if v) \
        or "none"


def _fmt_trans(d):
    return ", ".join(f"{k}={v}" for k, v in d["transition_totals"].items()
                     if v) or "none"


def cmd_dryrun_all(args):
    from _db import get_readonly_conn
    manifest = load_manifest()
    out = ["# Task 4a dry-run: manifest candidates vs stored edition 1", "",
           "Generated by `s1_editions.py dryrun-all`. Nothing written to any "
           "table.", "",
           "A = release-page attachment, B = registry file (the manifest's two "
           "candidates per period). Gate-8 feed: authorities (of 296) where "
           "extracted `support_needs_total` equals S1b "
           "`la_homelessness_support_needs` category "
           "`hh_one_or_more_support_needs` (A3 'households with one or more "
           "support needs'; NULL equals NULL).", ""]
    conn = get_readonly_conn()
    try:
        with conn.cursor() as cur:
            for period in STALE_PERIODS:
                ents = [e for e in manifest if e["period"] == period]
                if len(ents) != 2:
                    halt(f"{period}: {len(ents)} manifest entries, expected 2")
                s1b = _s1b_needs(cur, period)
                cur.execute(f"SELECT DISTINCT edition FROM public.{TABLE} "
                            "WHERE period = %s", (period,))
                eds = sorted(r[0] for r in cur.fetchall())
                cur.execute("SELECT DISTINCT source_edition FROM "
                            "public.la_homelessness_support_needs "
                            "WHERE period = %s", (period,))
                s1b_src = [r[0] for r in cur.fetchall()]
                e1 = _edition_map(cur, period, 1)
                eq1, _ = _needs_match({k: v["support_needs_total"]
                                       for k, v in e1.items()}, s1b)
                out += [f"## {period}", "",
                        f"Stored editions: {eds}. S1b source file: {s1b_src}. "
                        f"Stored edition 1 support_needs_total equals S1b for "
                        f"{eq1}/296.", ""]
                got = {}
                for tag, e in zip("AB", ents):
                    recs, _ = _extract_entry(e)
                    got[tag] = recs
                    d = diff_records(recs, cur, period, 1)
                    m = {r["lad24cd"]: r.get("support_needs_total")
                         for r in recs}
                    eq, ne = _needs_match(m, s1b)
                    out += [f"### Candidate {tag} vs edition 1: `{e['file']}` "
                            f"({e['release_label']}, last_modified "
                            f"{e['last_modified']})", "",
                            f"- authorities changed: "
                            f"{d['authorities_changed']} of 296; "
                            f"no_change: {d['no_change']}",
                            f"- cells changed: {_fmt_cells(d)}",
                            f"- transitions (all measures): {_fmt_trans(d)}",
                            "- households_in_ta transitions: "
                            f"{d['transitions'].get('households_in_ta', 'none')}",
                            "- gate-8 feed: support_needs_total equals S1b for "
                            f"{eq}/296 authorities ({ne} differ)", ""]
                ma = {r["lad24cd"]: {m: r.get(m) for m in MEASURES}
                      for r in got["A"]}
                mb = {r["lad24cd"]: {m: r.get(m) for m in MEASURES}
                      for r in got["B"]}
                dab = _diff_maps(mb, ma)
                out += ["### A vs B pairwise (B against A)", "",
                        f"- values identical: {dab['no_change']}",
                        f"- authorities differing: {dab['authorities_changed']}"
                        f"; cells: {_fmt_cells(dab)}",
                        f"- transitions: {_fmt_trans(dab)}", ""]
    finally:
        conn.close()
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"wrote {path}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ddl", help="create the table and triggers if absent"
                   ).set_defaults(func=cmd_ddl)
    sub.add_parser("backfill", help="record edition 1 of stored quarters and "
                   "the 2025Q2 revision").set_defaults(func=cmd_backfill)
    d = sub.add_parser("diff", help="diff two stored editions of a period")
    d.add_argument("--period", required=True)
    d.add_argument("--new", type=int, required=True)
    d.add_argument("--old", type=int, required=True)
    d.set_defaults(func=cmd_diff)
    ld = sub.add_parser("load", help="extract a manifest file and diff it "
                        "(dry-run); insert only with --commit")
    ld.add_argument("--period", required=True)
    ld.add_argument("--manifest-entry", type=int, required=True,
                    help="0-based index into s1_editions_manifest.json")
    ld.add_argument("--supersedes", type=int)
    ld.add_argument("--published-date", help="YYYY-MM-DD; default manifest "
                    "last_modified date")
    ld.add_argument("--commit", action="store_true",
                    help="insert the edition (append-only, irreversible)")
    ld.set_defaults(func=cmd_load)
    da = sub.add_parser("dryrun-all", help="markdown dry-run report of every "
                        "manifest candidate; writes no table")
    da.add_argument("--out", required=True)
    da.set_defaults(func=cmd_dryrun_all)
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
