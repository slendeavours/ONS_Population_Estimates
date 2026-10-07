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
    python scripts/ro4_editions.py load --financial-year FY
        (--manifest-label LABEL | --manifest-entry N | --manifest-file F)
        [--supersedes N]
        [--commit | --simulate]
        # a later release of a year already held. DRY-RUN by default: parses
        # the manifest entry's local file with s2_ro4_load.parse and prints a
        # plain diff against the year's latest edition (authorities changed,
        # cells changed per measure, NULL <-> value, value <-> value,
        # data_missing flips, national sums). --commit inserts it as the next
        # edition and runs the load gates in the same transaction (authority
        # count derived, codes in la_code_lookup, every measure present, and
        # NULL/zero/data_missing re-read from the raw file by an independent
        # route), rolling back on failure; --simulate does the same and always
        # rolls back. --supersedes must equal the chain tip. A file whose
        # sha256 is already recorded for the year is 'already loaded'. A file
        # whose own Front_Page does not say what the manifest's
        # release_label_actual / published_date_actual say it is is refused.
    python scripts/ro4_editions.py sync-new [--expected-authorities N]
                                            [--commit | --simulate]
        # records every live financial year that has no editions as edition 1
        # 'as loaded'; idempotent; DRY-RUN by default. Refuses a year whose
        # authority count is not the derived one. --expected-authorities is
        # only needed (and only accepted) when no live year has editions yet.
    python scripts/ro4_editions.py refresh-latest [--commit | --simulate]
                                                  [--accept-drift FY]
        # copies the latest edition into ro4_housing_expenditure for every
        # financial year whose live rows differ from it (the eleven measures,
        # la_name, data_missing and source only; never loaded_at or any other
        # column). DRY-RUN by default. --commit takes a per-year content hash
        # before and after in the same transaction (only the years being
        # refreshed may change, and only in the refresh columns) and re-checks
        # that each refreshed year equals its edition; it rolls back on any
        # failure. --simulate does the same and always rolls back. A year whose
        # live rows equal no stored edition is drift: the command halts unless
        # --accept-drift FY names it. Rows in only one of live and the edition
        # halt (an UPDATE cannot repair them).

A later release goes through `load` with a manifest entry
(scripts/ro4_editions_manifest.json) and a file placed in data/reference/, then
`refresh-latest`; s2_ro4_load.py is the parser and first-load tool only. The
release a year's edition 1 records is whatever the live rows were loaded as:
earlier releases that were never held cannot be placed before it.
"""
import argparse
import hashlib
import json
import re
import sys
import warnings
from collections import Counter
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from s1_editions import (classify_period, format_status as _format_status,  # noqa: E402
                         guard_problems, halt,
                         latest_edition as _s1_latest_edition, latest_map,
                         live_period_counts, modal_count, period_hashes,
                         rows_differing, sha256_file)
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


def manifest_entries(fy: str) -> list:
    """The manifest entries of one financial year, in manifest order."""
    return [e for e in load_manifest() if e[PERIOD] == fy]


def select_entry(manifest, fy, label=None, index=None, file=None) -> dict:
    """Manifest entry chosen by stored release label, 0-based index or file
    name; exactly one selector. A label shared by two entries of the year is
    refused as ambiguous (use --manifest-file or --manifest-entry)."""
    if sum(x is not None for x in (label, index, file)) != 1:
        halt("give exactly one of --manifest-label, --manifest-entry and "
             "--manifest-file")
    if label is not None:
        hits = [e for e in manifest
                if e[PERIOD] == fy and e["release_label_stored"] == label]
        if len(hits) > 1:
            halt(f"{len(hits)} manifest entries for {fy} share the stored "
                 f"label {label!r} ({[e['file'] for e in hits]}); the label "
                 "is ambiguous, select by --manifest-file or --manifest-entry")
        if len(hits) != 1:
            halt(f"0 manifest entries for {fy} with stored label {label!r}, "
                 "expected exactly 1")
        return hits[0]
    if file is not None:
        hits = [e for e in manifest if e[PERIOD] == fy and e["file"] == file]
        if len(hits) != 1:
            halt(f"{len(hits)} manifest entries for {fy} with file {file!r}, "
                 "expected exactly 1")
        return hits[0]
    if not 0 <= index < len(manifest):
        halt(f"--manifest-entry {index} outside 0..{len(manifest) - 1}")
    if manifest[index][PERIOD] != fy:
        halt(f"manifest entry {index} is {manifest[index][PERIOD]}, not {fy}")
    return manifest[index]


def source_text(entry: dict) -> str:
    """The text the live `source` column carries for a release: the same form
    s2_ro4_load.FILES uses, built from what the file really is (its cover
    sheet), not from the label stored in the editions table."""
    return (f"MHCLG Revenue Outturn RO4 {entry[PERIOD]}, "
            f"{entry['release_label_actual']}")


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
        la_name        varchar(100),
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
    # a table made by the first version of this module has la_name unbounded;
    # the live table's is varchar(100). A column type change is schema, not a
    # row update: it fires no row trigger and rewrites no value.
    cur.execute("""SELECT character_maximum_length FROM information_schema.columns
                   WHERE table_schema = 'public' AND table_name = %s
                     AND column_name = 'la_name'""", (t,))
    if cur.fetchone()[0] != 100:
        cur.execute(f"ALTER TABLE public.{t} ALTER COLUMN la_name TYPE "
                    "varchar(100)")
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
    """Every stored edition: one label, date and sha256 for the whole edition
    (and one source_file, or none). Edition 1 is either a manifest file (its
    sha256 is an entry's and label, date and file equal that entry's) or 'as
    loaded' (the sha256 is the canonical hash of the stored rows). A later
    edition is only ever loaded from a manifest entry, so its sha256 must be
    one. Local manifest files that are held must match their manifest sha256.
    -> (problems, info lines)."""
    cur.execute(f"""SELECT {PERIOD}, edition, COUNT(DISTINCT release_label),
                    COUNT(DISTINCT published_date), COUNT(DISTINCT source_sha256),
                    COUNT(DISTINCT source_file), MIN(release_label),
                    MIN(published_date), MIN(source_file), MIN(source_sha256)
                    FROM public.{TABLE} GROUP BY 1, 2 ORDER BY 1, 2""")
    bad, info = [], []
    for fy, ed, nl, nd, ns, nf, label, pub, src, sha in cur.fetchall():
        tag = f"{fy} ed{ed}"
        if (nl, nd, ns, nf) != (1, 1, 1, 1) and not (nl == nd == ns == 1
                                                     and nf == 0):
            bad.append(f"{tag}: {nl} labels, {nd} dates, {ns} shas, "
                       f"{nf} files")
            continue
        entry = next((e for e in manifest_entries(fy) if e["sha256"] == sha),
                     None)
        if entry is not None:
            kind = "the manifest file"
            if (label, pub.isoformat() if pub else None, src) != (
                    entry["release_label_stored"],
                    entry["published_date_stored"], entry["file"]):
                bad.append(f"{tag}: records the manifest file's sha but its "
                           "label/date/file differ from the manifest")
        elif ed == 1:
            kind = "as loaded (canonical rows hash)"
            rows = list(edition_rows(cur, fy, 1).values())
            if sha != rows_sha256(rows):
                bad.append(f"{tag}: sha256 is neither a manifest file's nor "
                           "the canonical hash of the stored rows")
        else:
            kind = "NOT a manifest file"
            bad.append(f"{tag}: sha256 is not any manifest entry's; a later "
                       "edition is only loaded from the manifest")
        info.append(f"{tag} = {kind}, {pub}, {label!r}")
    for e in load_manifest():
        p = REF_DIR / e["file"]
        if p.exists() and sha256_file(p) != e["sha256"]:
            bad.append(f"manifest {e['file']}: local file sha256 differs from "
                       "the manifest")
    return bad, info


# ------------------------------------------------------------ file re-parse

def parse_file(cur, fy, entry=None):
    """The independent parse of a local publisher file (s2_ro4_load.parse,
    header-driven); a DataFrame indexed by lad24cd. With a manifest `entry`
    that file, sheet and source text are parsed (the parser's own FILES table
    holds only the releases it first loaded); otherwise the parser's own."""
    import s2_ro4_load
    warnings.filterwarnings("ignore")
    spec = None if entry is None else dict(entry, source=source_text(entry))
    df, *_ = s2_ro4_load.parse(fy, cur.connection, spec)
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
    the stored edition it was recorded as (the edition whose sha256 is the
    entry's), else with edition 1, independent of the live table. Each result
    has fy, file, comparison, summary and problem ('' if the comparison is
    consistent with how the edition was recorded: a recorded file equals its
    re-parse; a file that is not recorded is merely compared with edition 1,
    and must not equal an edition 1 that was recorded 'as loaded')."""
    out = []
    for e in load_manifest():
        fy, path = e[PERIOD], REF_DIR / e["file"]
        if not path.exists():
            out.append({"fy": fy, "file": e["file"], "comparison": None,
                        "summary": f"{fy}: {e['file']} not held locally",
                        "problem": ""})
            continue
        cur.execute(f"""SELECT DISTINCT edition FROM public.{TABLE}
                        WHERE {PERIOD} = %s AND source_sha256 = %s""",
                    (fy, e["sha256"]))
        recorded = [r[0] for r in cur.fetchall()]
        ed = recorded[0] if recorded else 1
        cmp = compare_parsed(parse_file(cur, fy, e), edition_rows(cur, fy, ed))
        problem = ""
        if recorded and not cmp["equal"]:
            problem = (f"{fy}: edition {ed} records the file but the re-parse "
                       f"{describe(cmp)}")
        if not recorded and cmp["equal"]:
            cur.execute(f"""SELECT DISTINCT source_sha256 FROM public.{TABLE}
                            WHERE {PERIOD} = %s AND edition = 1""", (fy,))
            if {r[0] for r in cur.fetchall()} != {e["sha256"]}:
                problem = (f"{fy}: the re-parse equals edition 1 but edition 1 "
                           "was recorded as loaded, not as the file")
        out.append({"fy": fy, "file": e["file"], "comparison": cmp,
                    "summary": f"{fy} {e['file']} vs edition {ed}: "
                               f"{describe(cmp)} (file "
                               f"{'recorded as edition ' + str(ed) if recorded else 'not yet loaded'})",
                    "problem": problem})
    return out


# ------------------------------------------------------- cover-sheet check

FRONT_PAGE = re.compile(r"(\w+) release\W+which was published on "
                        r"(\d{1,2} [A-Za-z]+ \d{4})", re.I)


def front_page_release(path: Path) -> set:
    """{(ordinal word, date)} of the release sentences on the file's own
    Front_Page sheet, e.g. ('third', date(2026, 6, 11)). Read straight from the
    workbook, not from the manifest or the database."""
    import pandas as pd
    warnings.filterwarnings("ignore")
    fp = pd.read_excel(path, sheet_name="Front_Page", engine="odf", header=None)
    out = set()
    for v in fp.stack().astype(str):
        for hit in FRONT_PAGE.finditer(v):
            out.add((hit.group(1).lower(),
                     datetime.strptime(hit.group(2), "%d %B %Y").date()))
    return out


def check_front_page(path: Path, entry: dict) -> list:
    """Problems if the file's cover sheet does not say what the manifest's
    release_label_actual / published_date_actual say it is (or those two
    disagree with each other). This is the check that refuses a file filed
    under the wrong release."""
    tag = f"{entry['file']}"
    try:
        found = front_page_release(path)
    except Exception as e:  # unreadable workbook or no Front_Page sheet
        return [f"{tag}: cannot read the Front_Page ({type(e).__name__})"]
    if len(found) != 1:
        return [f"{tag}: Front_Page carries {len(found)} release sentences "
                f"{sorted(found)}, expected exactly 1"]
    (word, when), = found
    label, want = entry["release_label_actual"], entry["published_date_actual"]
    bad = []
    if not label.lower().startswith(f"{word} release"):
        bad.append(f"{tag}: cover sheet says '{word} release' but the "
                   f"manifest's actual label is {label!r}")
    if when.isoformat() != want:
        bad.append(f"{tag}: cover sheet says published {when}, manifest says "
                   f"{want}")
    tail = label.split("published", 1)[-1].strip()
    try:
        if datetime.strptime(tail, "%d %B %Y").date().isoformat() != want:
            bad.append(f"{tag}: actual label date {tail!r} is not {want}")
    except ValueError:
        bad.append(f"{tag}: actual label {label!r} has no 'published D Month "
                   "YYYY' date")
    return bad


def check_manifest_front_pages() -> list:
    """check_front_page for every manifest file held locally."""
    bad = []
    for e in load_manifest():
        p = REF_DIR / e["file"]
        if p.exists():
            bad += check_front_page(p, e)
    return bad


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


def record_edition1(cur, fy, want, plan_only=False) -> tuple:
    """Edition 1 of a live financial year that has none. The live rows must be
    `want` authorities sharing one load date and one source text. If the
    manifest's oldest entry for the year names a local file whose sha256
    matches and whose re-parse equals the live rows, edition 1 records that
    file; otherwise the live rows are recorded 'as loaded' (label and date from
    the live source text, canonical-rows sha256, no source_file). Returns
    (edition or None, rows, how)."""
    recs, loaded, sources = live_recs(cur, fy)
    if len(recs) != want or len({r["lad24cd"] for r in recs}) != want:
        halt(f"{fy}: {len(recs)} live rows, expected {want} (the derived "
             "count)")
    if len(loaded) != 1 or len(sources) != 1:
        halt(f"{fy}: live rows differ in loaded_at/source "
             f"({len(loaded)} dates, {len(sources)} sources)")
    entries = manifest_entries(fy)
    entry = entries[0] if entries else None  # the oldest release listed
    path = REF_DIR / entry["file"] if entry else None
    cmp = None
    if path is not None and path.exists():
        if sha256_file(path) != entry["sha256"]:
            halt(f"{entry['file']}: sha256 does not match the manifest")
        cmp = compare_parsed(parse_file(cur, fy, entry),
                             {r["lad24cd"]: r for r in recs})
    if cmp is not None and cmp["equal"]:
        kw = dict(release_label=entry["release_label_stored"],
                  published_date=date.fromisoformat(
                      entry["published_date_stored"]),
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
        return None, len(recs), how
    ed = insert_edition(cur, recs, fy, supersedes=None, **kw)
    cur.execute(f"SELECT COUNT(*) FROM public.{TABLE} WHERE {PERIOD} = %s "
                "AND edition = %s", (fy, ed))
    return ed, cur.fetchone()[0], how


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
        ed, n, how = record_edition1(cur, fy, want, plan_only)
        if plan_only:
            out.append((fy, 1, n, "would insert", how))
        else:
            out.append((fy, ed, n, "inserted", how))
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


# ------------------------------------------------------------------ load
#
# A later release of a year already held: parse its manifest file, diff it
# against the year's latest edition, insert it as the next edition and check it
# in the same transaction.

def _dec(v) -> "Decimal | None":
    """A parsed cell as the numeric(12,2) value the table will hold (NaN ->
    None; a real zero stays 0)."""
    if v is None or v != v:
        return None
    return Decimal(str(float(v))).quantize(Decimal("0.01"), ROUND_HALF_UP)


def recs_from_df(df, fy) -> list:
    """Records (dicts keyed by DATA_COLS) of a parsed file (DataFrame indexed
    by lad24cd, as parse_file returns)."""
    out = []
    for lad, r in df.iterrows():
        rec = {"lad24cd": lad, PERIOD: fy, "la_name": str(r["la_name"]),
               "data_missing": bool(r["data_missing"]),
               "source": str(r["source"])}
        rec.update({c: _dec(r[c]) for c in MEASURES})
        out.append(rec)
    return out


def diff_recs(recs: list, old: dict) -> dict:
    """Diff of new records against a stored edition ({lad: row dict}),
    read-only. Per measure: cells changed, NULL -> value, value -> NULL,
    value -> value, examples and the national sums on each side; plus the
    authorities that changed, data_missing flips both ways, name changes and
    authorities in one side only."""
    new = {r["lad24cd"]: r for r in recs}
    both = sorted(set(new) & set(old))
    per = {}
    for c in MEASURES:
        d = {"changed": 0, "null_to_value": 0, "value_to_null": 0,
             "value_to_value": 0, "examples": [],
             "old_sum": sum((o[c] for o in old.values() if o[c] is not None),
                            Decimal(0)),
             "new_sum": sum((n[c] for n in new.values() if n[c] is not None),
                            Decimal(0)),
             "old_n": sum(o[c] is not None for o in old.values()),
             "new_n": sum(n[c] is not None for n in new.values())}
        for lad in both:
            o, n = old[lad][c], new[lad][c]
            if o == n:
                continue
            d["changed"] += 1
            if o is None:
                d["null_to_value"] += 1
            elif n is None:
                d["value_to_null"] += 1
            else:
                d["value_to_value"] += 1
            d["examples"].append((lad, new[lad]["la_name"], o, n))
        per[c] = d
    changed = {x[0] for c in MEASURES for x in per[c]["examples"]}
    to_missing = [lad for lad in both
                  if new[lad]["data_missing"] and not old[lad]["data_missing"]]
    to_reported = [lad for lad in both
                   if old[lad]["data_missing"] and not new[lad]["data_missing"]]
    names = [(lad, old[lad]["la_name"], new[lad]["la_name"]) for lad in both
             if old[lad]["la_name"] != new[lad]["la_name"]]
    changed |= set(to_missing) | set(to_reported) | {x[0] for x in names}
    return {"per_measure": per, "authorities": sorted(changed),
            "to_missing": to_missing, "to_reported": to_reported,
            "names": names, "only_new": sorted(set(new) - set(old)),
            "only_old": sorted(set(old) - set(new)), "compared": len(both),
            "changed_cells": sum(d["changed"] for d in per.values())}


def format_diff(d: dict, names: "dict | None" = None, examples=2) -> str:
    """Plain-text lines of a diff_recs result (names: {lad: la_name})."""
    nm = names or {}
    out = [f"{len(d['authorities'])} of {d['compared']} authorities changed; "
           f"{d['changed_cells']} measure cells changed"]
    for c, p in d["per_measure"].items():
        if not p["changed"]:
            continue
        out.append(f"  {c}: {p['changed']} cells (NULL->value "
                   f"{p['null_to_value']}, value->NULL {p['value_to_null']}, "
                   f"value->value {p['value_to_value']}); national sum "
                   f"{p['old_sum']} -> {p['new_sum']} "
                   f"({p['new_sum'] - p['old_sum']:+})")
        for lad, name, o, n in p["examples"][:examples]:
            out.append(f"      e.g. {name or lad}: {o} -> {n}")
    flips = d["to_missing"] + d["to_reported"]
    out.append(f"  data_missing: {len(d['to_missing'])} became missing, "
               f"{len(d['to_reported'])} became reported"
               + (": " + ", ".join(nm.get(x, x) for x in flips)
                  if flips else ""))
    out.append(f"  la_name changes: {len(d['names'])}"
               + "".join(f"; {o!r} -> {n!r}" for _, o, n in d["names"][:3]))
    if d["only_new"] or d["only_old"]:
        out.append(f"  authorities only in the new file: {len(d['only_new'])}; "
                   f"only in the stored edition: {len(d['only_old'])}")
    return "\n".join(out)


# the file's own text for a cell that is not a figure
RAW_MARKERS = ("", "[x]", "[c]", "[z]", "[n]", "[low]", "..", "-")
# measure -> (header prefixes, a fragment the header must contain); lower case
_TOT, _NET = "total expenditure (c3", "net current expenditure (c7"
_NIGHTLY = ("nightly paid, privately managed accommodation, self-contained",)
RAW_HEADERS = {
    "nightly_paid_ta_gross_exp_000": (_NIGHTLY, _TOT),
    "nightly_paid_ta_net_exp_000": (_NIGHTLY, _NET),
    "hostels_gross_exp_000": (("hostels (including reception centres",), _TOT),
    "hostels_net_exp_000": (("hostels (including reception centres",), _NET),
    "bb_gross_exp_000": (("bed and breakfast hotels (including shared",), _TOT),
    "bb_net_exp_000": (("bed and breakfast hotels (including shared",), _NET),
    "hra_admin_prevention_relief_net_exp_000": (
        ("homeless reduction act: administration, prevention, relief",
         "homelessness services: administration, prevention, relief"), _NET),
    "total_homelessness_gross_exp_000": (("total homelessness",), _TOT),
    "total_homelessness_net_exp_000": (("total homelessness",), _NET),
    "total_housing_gross_exp_000": (("total housing services (gfra only)",),
                                    _TOT),
    "total_housing_net_exp_000": (("total housing services (gfra only)",), _NET),
}
AUTHORITY_CODE = re.compile(r"^E0[6-9]\d{6}$")
# the codes the publisher renamed; the table keeps the earlier ones
RAW_RENAMED = {"E08000038": "E08000016", "E08000039": "E08000019"}


def raw_cells(path: Path, sheet: str) -> dict:
    """The year's raw cells read without s2_ro4_load.parse: the header row is
    found by its 'ONS Code' cell, each measure column by its header text (one
    match each, or halt), and each cell is classified here, not coerced: a
    number is a value (0 stays 0), a documented marker or a blank is NULL,
    anything else halts. -> {lad24cd: {column: Decimal | None, 'la_name': str}}
    for every row whose code is authority-shaped."""
    import pandas as pd
    warnings.filterwarnings("ignore")
    raw = pd.read_excel(path, sheet_name=sheet, engine="odf", header=None)
    hdr_rows = [i for i in range(min(len(raw), 30))
                if str(raw.iat[i, 1]).strip() == "ONS Code"]
    if len(hdr_rows) != 1:
        halt(f"{path.name}: {len(hdr_rows)} header rows with 'ONS Code' in "
             "column 2")
    h = hdr_rows[0]
    heads = [str(v).strip().lower() for v in raw.iloc[h]]
    name_col = [i for i, t in enumerate(heads) if t == "local authority"]
    if len(name_col) != 1:
        halt(f"{path.name}: {len(name_col)} 'Local authority' columns")
    pos = {}
    for col, (prefixes, frag) in RAW_HEADERS.items():
        hit = [i for i, t in enumerate(heads)
               if t.startswith(prefixes) and frag in t]
        if len(hit) != 1:
            halt(f"{path.name}: {col} matches {len(hit)} headers")
        pos[col] = hit[0]
    out = {}
    for i in range(h + 1, len(raw)):
        code = str(raw.iat[i, 1]).strip()
        code = RAW_RENAMED.get(code, code)
        if not AUTHORITY_CODE.match(code):
            continue
        row = {"la_name": str(raw.iat[i, name_col[0]]).strip()}
        for col, j in pos.items():
            v = raw.iat[i, j]
            if isinstance(v, bool):
                halt(f"{path.name} {code} {col}: boolean cell {v!r}")
            if isinstance(v, (int, float)):
                row[col] = None if v != v else Decimal(str(v))
            elif str(v).strip().lower() in RAW_MARKERS:
                row[col] = None
            else:
                halt(f"{path.name} {code} {col}: cell {v!r} is neither a "
                     "number nor a documented marker")
        if code in out:
            halt(f"{path.name}: duplicate code {code}")
        out[code] = row
    return out


def check_raw_integrity(cur, fy, edition, raw=None) -> tuple:
    """Every stored cell of the edition equals an independent re-read of its
    raw cell: a figure is stored as that figure (rounded to the cell), a marker
    or blank as NULL (never 0), a real 0 as 0; data_missing is exactly 'total
    homelessness gross is NULL'; la_name is the file's; the stored authorities
    are exactly the authority-coded rows of the file that the table knows.
    raw defaults to the edition's own manifest file.
    -> (problems, cells checked, stored zeros)."""
    tag = f"{fy} ed{edition}"
    cur.execute(f"SELECT DISTINCT source_sha256, source_file FROM "
                f"public.{TABLE} WHERE {PERIOD} = %s AND edition = %s",
                (fy, edition))
    ids = cur.fetchall()
    if raw is None:
        entry = next((e for sha, _ in ids for e in manifest_entries(fy)
                      if e["sha256"] == sha), None) if len(ids) == 1 else None
        if entry is None or not (REF_DIR / entry["file"]).exists():
            return [f"{tag}: no local manifest file for {ids}"], 0, 0
        raw = raw_cells(REF_DIR / entry["file"], entry["sheet"])
    stored = edition_rows(cur, fy, edition)
    cur.execute("SELECT lad24cd FROM public.la_boundaries")
    known = {r[0] for r in cur.fetchall()}
    bad, cells, zeros = [], 0, 0
    absent = sorted(set(stored) - set(raw))
    extra = sorted((set(raw) & known) - set(stored))
    if absent or extra:
        bad.append(f"{tag}: {len(absent)} stored authorities not in the file "
                   f"{absent[:3]}, {len(extra)} file authorities known to the "
                   f"table but not stored {extra[:3]}")
    for lad in sorted(set(stored) & set(raw)):
        s, r = stored[lad], raw[lad]
        for c in MEASURES:
            cells += 1
            want = (None if r[c] is None else
                    r[c].quantize(Decimal("0.01"), ROUND_HALF_UP))
            zeros += s[c] == 0
            if s[c] != want:
                bad.append(f"{tag} {lad} {c}: stored {s[c]}, raw cell -> "
                           f"{want}")
        if s["data_missing"] != (r["total_homelessness_gross_exp_000"] is None):
            bad.append(f"{tag} {lad}: data_missing {s['data_missing']} but "
                       "the raw total homelessness gross is "
                       f"{r['total_homelessness_gross_exp_000']}")
        if s["la_name"] != r["la_name"]:
            bad.append(f"{tag} {lad}: la_name {s['la_name']!r}, file "
                       f"{r['la_name']!r}")
    return bad, cells, zeros


def check_loaded(cur, fy, edition, known_codes=None) -> list:
    """The structural gate of one edition: the derived authority count in
    authorities and rows, every lad24cd inside la_code_lookup, every measure
    column present and populated for some authority, data_missing consistent
    with NULL, a name, label and sha256 recorded."""
    cur.execute(f"""SELECT lad24cd, la_name, release_label, source_file,
                           source_sha256 FROM public.{TABLE}
                    WHERE {PERIOD} = %s AND edition = %s""", (fy, edition))
    rows = cur.fetchall()
    if known_codes is None:
        cur.execute("SELECT new_code FROM public.la_code_lookup UNION "
                    "SELECT old_code FROM public.la_code_lookup")
        known_codes = {r[0] for r in cur.fetchall()}
    tag = f"{fy} ed{edition}"
    bad = []
    n, want = len({r[0] for r in rows}), expected_authorities(cur)
    if (n, len(rows)) != (want, want):
        bad.append(f"{tag}: {n} authorities/{len(rows)} rows, expected "
                   f"{want}/{want}")
    orphans = sorted({r[0] for r in rows} - set(known_codes))
    if orphans:
        bad.append(f"{tag}: {len(orphans)} lad24cd outside la_code_lookup "
                   f"{orphans[:3]}")
    if any(r[1] is None or r[2] is None or r[4] is None for r in rows):
        bad.append(f"{tag}: la_name, release_label or source_sha256 missing")
    cur.execute("""SELECT column_name FROM information_schema.columns
                   WHERE table_schema = 'public' AND table_name = %s""",
                (TABLE,))
    cols = {r[0] for r in cur.fetchall()}
    absent = [c for c in MEASURES if c not in cols]
    if absent:
        bad.append(f"{tag}: measure columns absent from the table {absent}")
    else:
        counts = ", ".join(f"COUNT({c})" for c in MEASURES)
        cur.execute(f"""SELECT {counts} FROM public.{TABLE}
                        WHERE {PERIOD} = %s AND edition = %s""", (fy, edition))
        empty = [c for c, k in zip(MEASURES, cur.fetchone()) if not k]
        if empty:
            bad.append(f"{tag}: measure columns with no value for any "
                       f"authority {empty}")
    cur.execute(f"""SELECT COUNT(*) FROM public.{TABLE}
                    WHERE {PERIOD} = %s AND edition = %s AND data_missing IS
                    DISTINCT FROM (total_homelessness_gross_exp_000 IS NULL)""",
                (fy, edition))
    k = cur.fetchone()[0]
    if k:
        bad.append(f"{tag}: {k} rows where data_missing disagrees with a NULL "
                   "total homelessness gross")
    return bad


def run_load_gates(cur, fy, edition, raw=None, entry=None) -> None:
    """The cover-sheet check (when the manifest entry is given), the authority
    count, la_code_lookup, measure presence and the independent raw re-read for
    the edition just inserted; halt on any problem."""
    problems = (check_front_page(REF_DIR / entry["file"], entry)
                if entry is not None else [])
    problems += check_loaded(cur, fy, edition)
    p2, _, _ = check_raw_integrity(cur, fy, edition, raw)
    problems += p2
    if problems:
        halt(f"{fy} edition {edition} failed its load gates, rolled back: "
             + "; ".join(problems[:5]))


def check_supersedes(wanted, tip, fy) -> None:
    """--supersedes, if given, must be the year's chain tip."""
    if wanted is not None and wanted != tip:
        halt(f"--supersedes {wanted} is not the current chain tip "
             f"(edition {tip}) of {fy}")


def cmd_load(args):
    from _db import get_conn, get_readonly_conn
    fy = args.financial_year
    entry = select_entry(load_manifest(), fy, args.manifest_label,
                         args.manifest_entry, args.manifest_file)
    path = REF_DIR / entry["file"]
    writing = args.commit or args.simulate
    conn = get_conn() if writing else get_readonly_conn()
    try:
        with conn.cursor() as cur:
            if not table_exists(cur):
                halt(f"{TABLE} does not exist; run ddl then backfill")
            if not path.exists():
                halt(f"{path} not found")
            sha = sha256_file(path)
            if sha != entry["sha256"]:
                halt(f"{entry['file']}: sha256 {sha} does not match manifest "
                     f"{entry['sha256']}")
            problems = check_front_page(path, entry)
            if problems:
                halt("cover sheet does not match the manifest, refusing to "
                     "load: " + "; ".join(problems))
            cur.execute(f"SELECT DISTINCT edition FROM public.{TABLE} "
                        f"WHERE {PERIOD} = %s AND source_sha256 = %s",
                        (fy, sha))
            have = [r[0] for r in cur.fetchall()]
            if have:
                print(f"already loaded (edition {have[0]}), nothing inserted")
                conn.rollback()
                return
            try:
                prev = latest_edition(cur, fy)
            except LookupError:
                halt(f"{fy} has no edition 1; run sync-new (or backfill) first")
            except ValueError as e:
                halt(f"cannot determine latest edition: {e}")
            check_supersedes(args.supersedes, prev, fy)
            recs = recs_from_df(parse_file(cur, fy, entry), fy)
            d = diff_recs(recs, edition_rows(cur, fy, prev))
            names = {r["lad24cd"]: r["la_name"] for r in recs}
            print(f"{fy} {entry['file']} (cover sheet: "
                  f"{entry['release_label_actual']}; stored as "
                  f"{entry['release_label_stored']!r}) vs stored edition "
                  f"{prev}:\n" + format_diff(d, names))
            if prev != 1:
                d1 = diff_recs(recs, edition_rows(cur, fy, 1))
                print("and vs stored edition 1:\n" + format_diff(d1, names))
            if not (d["changed_cells"] or d["to_missing"] or d["to_reported"]
                    or d["names"] or d["only_new"] or d["only_old"]):
                print("  NOTE: identical in values to the previous edition; "
                      "it would be recorded as an edition with no change")
            if not writing:
                print("DRY RUN: nothing written (use --commit or --simulate)")
                return
            ed = insert_edition(
                cur, recs, fy, release_label=entry["release_label_stored"],
                published_date=date.fromisoformat(
                    entry["published_date_stored"]),
                source_file=entry["file"], source_sha256=sha, supersedes=prev)
            if latest_edition(cur, fy) != ed:
                halt(f"{fy}: new edition {ed} is not the chain tip")
            run_load_gates(cur, fy, ed, entry=entry)
            cur.execute(f"SELECT COUNT(*) FROM public.{TABLE} WHERE "
                        f"{PERIOD} = %s AND edition = %s", (fy, ed))
            n = cur.fetchone()[0]
        if args.commit:
            conn.commit()
            print(f"loaded {fy} edition {ed} ({n} rows); load gates passed in "
                  "the same transaction. COMMITTED (refresh-latest is a "
                  "separate step)")
        else:
            conn.rollback()
            print(f"SIMULATION: {fy} edition {ed} inserted ({n} rows), load "
                  "gates passed, ROLLED BACK (nothing persisted)")
    except BaseException:
        if writing:
            conn.rollback()
        raise
    finally:
        conn.close()


# ------------------------------------------------- sync-new, refresh-latest
#
# Generic for any financial year: nothing below names a year or a row total.
# What needs doing is derived from the data (live vs the editions table), and a
# refresh is checked by a per-year before/after content hash in the same
# transaction.

def _refresh_where() -> str:
    return "(" + " OR ".join(f"l.{c} IS DISTINCT FROM e.{c}"
                             for c in REFRESH_COLS) + ")"


def sync_new(cur, expected_authorities_n=None) -> list:
    """Give every live financial year that has no editions its edition 1
    ('as loaded', or the manifest file if the live rows equal it). Never touches
    a year that already has editions, so it is idempotent. Halts on a year
    whose authority count is not the one derived from the years that have
    editions. Returns the years that were given an edition. If no live year has
    editions yet there is nothing to derive the count from, so
    expected_authorities_n (CLI --expected-authorities N) must be given; once a
    count can be derived N is accepted only if it equals it."""
    tips, new, errors = latest_map(cur, LIVE, TABLE, PERIOD)
    if errors:
        halt("sync-new: invalid edition chain " + "; ".join(
            f"{p}: {m}" for p, m in errors.items()))
    known = {p: n for p, n in live_period_counts(cur, LIVE, PERIOD).items()
             if p not in new}
    derived = modal_count(known)
    if (expected_authorities_n is not None and derived is not None
            and expected_authorities_n != derived):
        halt(f"sync-new: --expected-authorities {expected_authorities_n} "
             f"differs from the count {derived} derived from financial years "
             "that already have editions; it is only accepted when nothing "
             "can be derived (or when it equals the derived count)")
    expected = derived if derived is not None else expected_authorities_n
    if new and expected is None:
        halt("sync-new: no live financial year has editions, so the authority "
             "count cannot be derived; give --expected-authorities N")
    done = []
    for fy in sorted(new):
        record_edition1(cur, fy, expected)
        done.append(fy)
    return done


def _one_sided(cur, fy, edition) -> int:
    """Authorities in only one of the live year and the edition."""
    cur.execute(f"""SELECT
        (SELECT COUNT(*) FROM public.{LIVE} l WHERE l.{PERIOD} = %s AND NOT
            EXISTS (SELECT 1 FROM public.{TABLE} e WHERE e.{PERIOD} = %s
                    AND e.edition = %s AND e.lad24cd = l.lad24cd))
      + (SELECT COUNT(*) FROM public.{TABLE} e WHERE e.{PERIOD} = %s
            AND e.edition = %s AND NOT EXISTS (SELECT 1 FROM public.{LIVE} l
                    WHERE l.{PERIOD} = %s AND l.lad24cd = e.lad24cd))""",
                (fy, fy, edition, fy, edition, fy))
    return cur.fetchone()[0]


def _plan(cur, accept_drift=()) -> tuple:
    """({fy: {edition, kind, rows, one_sided}} for every financial year whose
    live rows differ from the latest edition, unaccepted drift years). kind is
    'pending' (live equals an earlier edition) or 'drift' (it equals none);
    rows is how many live rows an update would write, one_sided how many
    authorities are in only one of live and the edition."""
    tips, new, errors = latest_map(cur, LIVE, TABLE, PERIOD)
    if new:
        halt(f"financial years with no editions {new}; run sync-new first")
    if errors:
        halt("invalid edition chain: " + "; ".join(
            f"{p}: {m}" for p, m in errors.items()))
    plan, drift = {}, []
    for fy, tip in tips.items():
        kind, _ = classify_period(cur, LIVE, TABLE, KEY, REFRESH_COLS, fy, tip,
                                  PERIOD)
        if kind == "current":
            continue
        if kind == "drift" and fy not in accept_drift:
            drift.append(fy)
        plan[fy] = {"edition": tip, "kind": kind}
    stray = sorted(set(accept_drift) - {p for p, v in plan.items()
                                        if v["kind"] == "drift"})
    if stray:
        halt(f"--accept-drift {stray}: not drifted financial years, nothing "
             "to accept")
    for fy, v in plan.items():
        cur.execute(f"""SELECT COUNT(*) FROM public.{LIVE} l
                        JOIN public.{TABLE} e ON e.lad24cd = l.lad24cd
                         AND e.{PERIOD} = l.{PERIOD} AND e.edition = %s
                        WHERE l.{PERIOD} = %s AND {_refresh_where()}""",
                    (v["edition"], fy))
        v["rows"] = cur.fetchone()[0]
        v["one_sided"] = _one_sided(cur, fy, v["edition"])
    return plan, drift


def _unrepairable(plan) -> list:
    """Planned years with an authority in only one of live and the edition,
    or that differ with no row to update: an UPDATE cannot repair them."""
    return sorted(p for p, v in plan.items() if v["one_sided"] or not v["rows"])


def refresh_counts(cur, accept_drift=()) -> dict:
    """{fy: rows refresh_latest would write} (read-only)."""
    plan, _ = _plan(cur, accept_drift)
    return {p: v["rows"] for p, v in sorted(plan.items())}


def refresh_latest(cur, accept_drift=(), _after_update_hook=None) -> dict:
    """Copy the latest edition into the live table for every financial year
    whose live rows differ from it (NULL-safe); return a result dict.

    Updates ONLY the eleven measures, la_name, data_missing and source, on the
    rows of those years that differ. Every other column, including loaded_at,
    and every other year is untouched. Inside the caller's transaction the
    per-year content hash (excluding loaded_at) is taken before and after: a
    year not being refreshed must be identical, a refreshed one may differ only
    in the refresh columns; each refreshed year must then equal its latest
    edition on every refresh column. A year whose live rows equal no stored
    edition is drift: it halts unless named in accept_drift. Any failure halts
    (the caller rolls back). _after_update_hook(cur) is a test seam that runs
    after the UPDATE and before the after-checks.

    Result: {'updated': {fy: rows}, 'rows': n, 'drift_accepted': [...]}.
    """
    plan, drift = _plan(cur, accept_drift)
    if drift:
        halt("live differs from the latest edition and matches no stored "
             f"edition for {drift}: changed outside the editions table. "
             "Load it as an edition, or re-run with --accept-drift FY to "
             "overwrite it with the latest edition")
    stuck = _unrepairable(plan)
    if stuck:
        halt(f"{stuck} differ from the latest edition in rows present in only "
             "one of them; an update of the refresh columns cannot repair that")
    result = {"updated": {}, "rows": 0,
              "drift_accepted": sorted(p for p, v in plan.items()
                                       if v["kind"] == "drift")}
    if not plan:
        return result
    refreshed = set(plan)
    full_b = period_hashes(cur, LIVE, HASH_KEY, period_col=PERIOD)
    kept_b = period_hashes(cur, LIVE, HASH_KEY, exclude=REFRESH_COLS,
                           period_col=PERIOD)
    sets = ", ".join(f"{c} = e.{c}" for c in REFRESH_COLS)
    pairs = tuple((p, v["edition"]) for p, v in sorted(plan.items()))
    cur.execute(f"""UPDATE public.{LIVE} l SET {sets}
                    FROM public.{TABLE} e
                    WHERE e.lad24cd = l.lad24cd AND e.{PERIOD} = l.{PERIOD}
                      AND (e.{PERIOD}, e.edition) IN %s AND {_refresh_where()}""",
                (pairs,))
    n = cur.rowcount
    expected = sum(v["rows"] for v in plan.values())
    if _after_update_hook is not None:
        _after_update_hook(cur)
    full_a = period_hashes(cur, LIVE, HASH_KEY, period_col=PERIOD)
    kept_a = period_hashes(cur, LIVE, HASH_KEY, exclude=REFRESH_COLS,
                           period_col=PERIOD)
    bad = guard_problems(full_b, full_a, kept_b, kept_a, refreshed)
    if n != expected:
        bad.append(f"wrote {n} rows, expected {expected}")
    for fy, v in sorted(plan.items()):
        k = rows_differing(cur, LIVE, TABLE, KEY, REFRESH_COLS, fy,
                           v["edition"], PERIOD)
        if k:
            bad.append(f"{fy}: {k} rows still differ from edition "
                       f"{v['edition']} after the refresh")
    bad += [f"live!=latest: {x}"
            for x in check_latest_equals_live(cur, sorted(refreshed))]
    if bad:
        halt("refresh-latest failed its before/after checks, rolled back: "
             + "; ".join(bad[:6]))
    result["updated"] = {p: v["rows"] for p, v in sorted(plan.items())}
    result["rows"] = n
    return result


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


def cmd_sync_new(args):
    from _db import get_conn, get_readonly_conn
    writing = args.commit or args.simulate
    conn = get_conn() if writing else get_readonly_conn()
    try:
        with conn.cursor() as cur:
            if not table_exists(cur):
                halt(f"{TABLE} does not exist; run ddl then backfill")
            tips, new, errors = latest_map(cur, LIVE, TABLE, PERIOD)
            print("financial years with no editions: " + (", ".join(new)
                                                          or "none"))
            if not writing:
                print("DRY RUN: nothing written (use --commit or --simulate)")
                return
            done = sync_new(cur, args.expected_authorities)
            for fy in done:
                cur.execute(f"SELECT COUNT(*) FROM public.{TABLE} "
                            f"WHERE {PERIOD} = %s AND edition = 1", (fy,))
                print(f"  {fy}: edition 1 recorded, {cur.fetchone()[0]} rows")
            st = status(cur)
            if st["new_periods"] or st["chain_errors"]:
                halt(f"after sync-new: {format_status(st)}")
            bad, _, _ = check_coverage(cur)
            bad += check_null_not_zero(cur) + check_provenance(cur)[0]
            if bad:
                halt("sync-new failed its gates, rolled back: "
                     + "; ".join(bad[:6]))
        if args.commit:
            conn.commit()
            print(f"COMMITTED: {len(done)} financial year(s) recorded as "
                  "edition 1")
        else:
            conn.rollback()
            print("SIMULATION: ROLLED BACK (nothing persisted)")
    except BaseException:
        if writing:
            conn.rollback()
        raise
    finally:
        conn.close()


def cmd_refresh_latest(args):
    from _db import get_conn, get_readonly_conn
    writing = args.commit or args.simulate
    accept = tuple(args.accept_drift or ())
    conn = get_conn() if writing else get_readonly_conn()
    try:
        with conn.cursor() as cur:
            if not table_exists(cur):
                halt(f"{TABLE} does not exist; run ddl then backfill")
            plan, drift = _plan(cur, accept)
            print("rows refresh-latest would write: "
                  + (", ".join(f"{p}={v['rows']} (edition {v['edition']}, "
                               f"{v['kind']})" for p, v in sorted(plan.items()))
                     or "none")
                  + f" (total {sum(v['rows'] for v in plan.values())})")
            if drift:
                halt(f"drift: live differs from the latest edition and matches "
                     f"no stored edition for {drift}; load it as an edition or "
                     "pass --accept-drift FY to overwrite it")
            if _unrepairable(plan):
                halt(f"{_unrepairable(plan)} differ from the latest edition "
                     "in rows present in only one of them; an update cannot "
                     "repair that")
            if not writing:
                print("DRY RUN: nothing written (use --commit or --simulate)")
                return
            res = refresh_latest(cur, accept)
        if not res["rows"]:
            print("nothing to refresh")
        else:
            print(f"{res['rows']} live rows refreshed in "
                  f"{sorted(res['updated'])}; before/after guard passed")
            if res["drift_accepted"]:
                print(f"drift overwritten by request: {res['drift_accepted']}")
        if args.commit:
            conn.commit()
            print("COMMITTED")
        else:
            conn.rollback()
            print("SIMULATION: ROLLED BACK (nothing persisted)")
    except BaseException:
        if writing:
            conn.rollback()
        raise
    finally:
        conn.close()


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
    ld = sub.add_parser("load", help="parse a manifest file and diff it "
                        "against the latest edition (dry-run); insert only "
                        "with --commit")
    ld.add_argument("--financial-year", required=True)
    ld.add_argument("--manifest-label", help="select the year's manifest "
                    "entry by its STORED release label (refused if two "
                    "entries share it)")
    ld.add_argument("--manifest-file", help="select the manifest entry by "
                    "file name")
    ld.add_argument("--manifest-entry", type=int,
                    help="alternative: 0-BASED index into the manifest")
    ld.add_argument("--supersedes", type=int, help="must equal the current "
                    "chain tip of the year (default: the tip)")
    lm = ld.add_mutually_exclusive_group()
    lm.add_argument("--commit", action="store_true",
                    help="insert the edition (append-only, irreversible)")
    lm.add_argument("--simulate", action="store_true",
                    help="run the full --commit path and roll back")
    ld.set_defaults(func=cmd_load)
    sn = sub.add_parser("sync-new", help="record live financial years with no "
                        "editions as edition 1 (dry-run by default)")
    sn.add_argument("--expected-authorities", type=int, metavar="N",
                    help="authorities per year; required only when no live "
                    "year has editions yet (otherwise derived)")
    smode = sn.add_mutually_exclusive_group()
    smode.add_argument("--commit", action="store_true")
    smode.add_argument("--simulate", action="store_true",
                       help="do everything, then roll back")
    sn.set_defaults(func=cmd_sync_new)
    rl = sub.add_parser("refresh-latest", help="copy each year's latest "
                        "edition into the live table (dry-run by default)")
    rm = rl.add_mutually_exclusive_group()
    rm.add_argument("--commit", action="store_true",
                    help="refresh the live table (before/after guard in the "
                    "same transaction)")
    rm.add_argument("--simulate", action="store_true",
                    help="do everything, then roll back")
    rl.add_argument("--accept-drift", action="append", metavar="FY",
                    help="overwrite this year although its live rows equal no "
                    "stored edition (repeatable)")
    rl.set_defaults(func=cmd_refresh_latest)
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
