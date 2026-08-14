"""S1b - MHCLG statutory homelessness Table A3, support needs of household.

S1 already loads six A3 figures into la_statutory_homelessness. S1b loads the
whole of A3 into its own long-format table rather than widening that one.

Three findings from discovery drove that choice, and they are worth stating
because they are the argument for the shape of this file.

1. A3 publishes 24 individual support-need categories, six mutually exclusive
   household-breakdown columns and a needs-count total. Widening a wide table
   by nineteen columns puts the publisher's category list into the schema,
   where every publisher change becomes a migration.

2. MHCLG restructured A3 in the January to March 2026 release. The sheet went
   from 37 columns to 34, the header moved from a four-row merged block to a
   single labelled row, and every category label was rewritten - "Young person
   aged 16-17 years" became "Young person aged 16 to 17". The category set did
   not change. A long table keyed on a canonical category code absorbs that; a
   column-per-category schema would have needed renaming.

3. S1's five named support-need columns do not hold what their names say. In
   2025Q2 every one of them is four columns to the left of its label:
   mental_health holds "Care leaver aged 21-24", and Middlesbrough's true
   mental health figure of 297 is stored as 6. That is provable rather than
   inferred - homelessness_quarter_urls names the exact asset the quarter was
   loaded from, and it is byte-identical to the file this script reads, so no
   revision can account for it. In 2025Q3 the offset is different again.

The third finding is why extraction here is driven by labels and not by column
positions. Every data column in the sheet must match exactly one canonical
category, and every canonical category must match exactly one column, or the
build stops. A positional read cannot fail loudly; this one cannot fail
quietly. Nothing published ever read the S1 columns - staging_la_signals takes
only ta_households_* from S1 - so this script does not touch them. See
docs/decisions/2026-08-14-s1-support-need-column-misalignment.md.

Suppression is stored distinctly from zero, because a suppressed cell
becoming a zero is the failure mode most worth preventing. Both marker
vocabularies are documented in the publisher's own files:

    legacy  ..  authority with missing data        -> flag 'missing'
    legacy  -   breakdown suppressed, <5 households -> flag 'suppressed'
    v2026   [x] missing, non-submission or quality  -> flag 'missing'
    v2026   [c] suppressed, protects identification -> flag 'suppressed'
    v2026   [z] not applicable                      -> flag 'not_applicable'

A flagged cell stores NULL in value and the reason in value_flag. A zero
stores 0 and a NULL flag. The two can never be confused.

Usage:
    python scripts/s1b_support_needs_build.py --discover
    python scripts/s1b_support_needs_build.py --load
    python scripts/s1b_support_needs_build.py --load --period 2025Q4
"""
import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

import pandas as pd
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _db import get_conn  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parent.parent
RAW = REPO / "data" / "raw" / "s1b_a3"
TABLE = "la_homelessness_support_needs"
SOURCE_CODE = "1b"
UA = {"User-Agent": "ucws-pipeline/s1b (+sl@slendeavours.org)"}

LANDING = "https://www.gov.uk/government/collections/homelessness-statistics"
CONTENT_API = "https://www.gov.uk/api/content/government/statistics/"

# The publisher's own period is a calendar quarter; the pipeline's period key
# is the financial-year quarter already used by la_statutory_homelessness,
# where 2023Q2 is July to September 2023. Both are kept: period joins to the
# rest of the pipeline, reference_quarter says what the publisher called it.
RELEASES = {
    "2023Q2": ("july-to-september-2023", "2023-09"),
    "2023Q3": ("october-to-december-2023", "2023-12"),
    "2023Q4": ("january-to-march-2024", "2024-03"),
    "2024Q1": ("april-to-june-2024", "2024-06"),
    "2024Q2": ("july-to-september-2024", "2024-09"),
    "2024Q3": ("october-to-december-2024", "2024-12"),
    "2024Q4": ("january-to-march-2025", "2025-03"),
    "2025Q1": ("april-to-june-2025", "2025-06"),
    "2025Q2": ("july-to-september-2025", "2025-09"),
    "2025Q3": ("october-to-december-2025", "2025-12"),
    "2025Q4": ("january-to-march-2026", "2026-03"),
}

# Matched against the normalised header text of each column. Ordered, and the
# order is load-bearing only for readability - a column matching two patterns
# is an error, not a precedence question.
SUPPORT_NEEDS = [
    ("young_person_16_17", r"young person aged 16 ?(?:-|to) ?17"),
    ("young_person_18_25", r"young person aged 18 ?(?:-|to) ?25"),
    ("young_parent", r"young parent"),
    ("care_leaver_18_20", r"care leaver aged 18 ?(?:-|to) ?20"),
    ("care_leaver_21_24", r"care leaver aged 21 ?(?:-|to) ?24"),
    ("care_leaver_25_plus", r"care leaver aged 25 ?(?:\+|or over)"),
    ("care_leaver_legacy_combined", r"care leaver.*(?:retired option|legacy combined)"),
    ("physical_ill_health_disability", r"physical ill health"),
    ("mental_health_history", r"history of mental health"),
    ("learning_disability", r"learning disability"),
    ("sexual_abuse", r"sexual abuse"),
    ("domestic_abuse", r"experienced domestic abuse"),
    ("non_domestic_abuse", r"abuse \(non ?-? ?domestic"),
    ("drug_dependency", r"drug dependency"),
    ("alcohol_dependency", r"alcohol dependency"),
    ("offending_history", r"offending history"),
    ("repeat_homelessness_history", r"history of repeat homelessness"),
    ("rough_sleeping_history", r"history of rough sleeping"),
    ("former_asylum_seeker", r"former asylum seeker"),
    ("old_age", r"old age"),
    ("served_in_hm_forces", r"served in hm forces"),
    ("access_to_education_employment_training", r"access to education"),
    ("modern_slavery_victim", r"victim of modern slavery"),
    ("difficulties_budgeting", r"difficulties budgeting"),
]

# Mutually exclusive household counts. These DO sum to the duty total, unlike
# the 24 above, so they carry a different category_group and a consumer can
# tell which arithmetic is legitimate without reading the documentation.
BREAKDOWN = [
    ("hh_no_support_needs", r"households with no support needs"),
    ("hh_unknown_support_needs", r"households with unknown support needs"),
    ("hh_one_or_more_support_needs", r"households with one or more support needs"),
]
TOTALS = [
    ("total_support_needs_count",
     r"total number of support needs|number of support needs ?total"),
    ("hh_owed_prevention_or_relief_duty",
     r"owed a prevention or relief duty ?total"),
]
# Legacy lays 1 / 2 / 3+ out as a merged block whose only distinguishing text
# is the digit itself, so they are located by position from the "one or more"
# column and then asserted against that text. v2026 labels them in full.
COUNT_BUCKETS = [
    ("hh_one_support_need", "1", r"households with one support need"),
    ("hh_two_support_needs", "2", r"households with two support needs"),
    ("hh_three_or_more_support_needs", "3+", r"households with three or more support needs"),
]

GROUP = {}
for code, _ in SUPPORT_NEEDS:
    GROUP[code] = "support_need"
for code, _ in BREAKDOWN:
    GROUP[code] = "needs_breakdown"
for code, _, _ in COUNT_BUCKETS:
    GROUP[code] = "needs_breakdown"
GROUP["total_support_needs_count"] = "needs_total"
GROUP["hh_owed_prevention_or_relief_duty"] = "duty_total"

FLAGS = {"..": "missing", "-": "suppressed",
         "[x]": "missing", "[c]": "suppressed", "[z]": "not_applicable"}

DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    lad24cd            varchar(9)  NOT NULL,
    period             varchar(6)  NOT NULL,
    category_code      text        NOT NULL,
    value              integer,
    value_flag         text,
    category_group     text        NOT NULL,
    category_label     text        NOT NULL,
    reference_quarter  varchar(7)  NOT NULL,
    source_url         text        NOT NULL,
    source_edition     text        NOT NULL,
    edition_variant    text        NOT NULL,
    release_page_url   text        NOT NULL,
    layout_version     text        NOT NULL,
    publisher_la_code  varchar(9)  NOT NULL,
    loaded_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (lad24cd, period, category_code)
);
"""

DDL_GUARDS = f"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = '{TABLE}_value_flag_chk') THEN
        ALTER TABLE {TABLE} ADD CONSTRAINT {TABLE}_value_flag_chk
            CHECK (value_flag IS NULL
                   OR value_flag IN ('missing','suppressed','not_applicable'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = '{TABLE}_value_xor_flag_chk') THEN
        ALTER TABLE {TABLE} ADD CONSTRAINT {TABLE}_value_xor_flag_chk
            CHECK (num_nonnulls(value, value_flag) = 1);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = '{TABLE}_category_group_chk') THEN
        ALTER TABLE {TABLE} ADD CONSTRAINT {TABLE}_category_group_chk
            CHECK (category_group IN
                   ('support_need','needs_breakdown','needs_total','duty_total'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = '{TABLE}_edition_variant_chk') THEN
        ALTER TABLE {TABLE} ADD CONSTRAINT {TABLE}_edition_variant_chk
            CHECK (edition_variant IN
                   ('original','revised','corrected','fixed'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_indexes
                   WHERE indexname = '{TABLE}_period_category_idx') THEN
        CREATE INDEX {TABLE}_period_category_idx
            ON {TABLE} (period, category_code);
    END IF;
END $$;
"""

UPSERT = f"""
INSERT INTO {TABLE} (
    lad24cd, period, category_code, value, value_flag, category_group,
    category_label, reference_quarter, source_url, source_edition,
    edition_variant, release_page_url, layout_version, publisher_la_code)
VALUES %s
ON CONFLICT (lad24cd, period, category_code) DO UPDATE SET
    value             = EXCLUDED.value,
    value_flag        = EXCLUDED.value_flag,
    category_group    = EXCLUDED.category_group,
    category_label    = EXCLUDED.category_label,
    reference_quarter = EXCLUDED.reference_quarter,
    source_url        = EXCLUDED.source_url,
    source_edition    = EXCLUDED.source_edition,
    edition_variant   = EXCLUDED.edition_variant,
    release_page_url  = EXCLUDED.release_page_url,
    layout_version    = EXCLUDED.layout_version,
    publisher_la_code = EXCLUDED.publisher_la_code,
    loaded_at         = now();
"""


def halt(msg):
    sys.exit(f"HALT: {msg}")


def norm(text):
    """Normalise a header cell for label matching.

    v2026 footnote markers ("[note 12]") are removed because they sit inside
    the label text. Legacy trailing footnote digits ("Care leaver aged 21-24
    3") are deliberately left alone: every pattern below is a substring match
    that does not anchor at the end, so a trailing footnote is harmless, while
    stripping trailing digits would eat the "17" out of "aged 16 to 17".
    """
    s = str(text).replace("–", "-").replace("’", "'")
    s = s.replace("�", "-")
    s = re.sub(r"\[note \d+\]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s.lower()


def edition_variant(filename):
    low = filename.lower()
    for token in ("corrected", "revised"):
        if token in low:
            return token
    if "fix" in low:
        return "fixed"
    return "original"


_EDITION_CACHE = {}
_A3_CACHE = {}


def resolve_edition(period):
    """Resolve the current detailed-LA attachment from the release page.

    URLs are never taken from the database. homelessness_quarter_urls holds
    _revised assets for four quarters that still resolve but are no longer
    linked from any release page, so the register cannot be trusted to say
    what the publisher currently serves. Preference runs corrected > revised >
    fix > original; the "accessible" rendering is excluded because it is the
    same data in a second layout.
    """
    if period in _EDITION_CACHE:
        return _EDITION_CACHE[period]
    slug, ref_q = RELEASES[period]
    with urllib.request.urlopen(
            urllib.request.Request(CONTENT_API +
                                   "statutory-homelessness-in-england-" + slug,
                                   headers=UA), timeout=60) as fh:
        doc = json.load(fh)
    rank = {"corrected": 4, "revised": 3, "fixed": 2, "original": 1}
    best = None
    for att in doc["details"].get("attachments", []):
        url = str(att.get("url", ""))
        if not url.startswith("http"):
            continue
        name = url.rsplit("/", 1)[-1]
        if not re.search(r"detailed_la|detailed_local_authority", name, re.I):
            continue
        if "multiple_disadvantage" in name.lower() or "MDIS" in name:
            continue
        if "accessible" in name.lower():
            continue
        score = rank[edition_variant(name)]
        if best is None or score > best[0]:
            best = (score, url, name)
    if best is None:
        halt(f"{period}: no detailed local authority attachment on {slug}")
    _EDITION_CACHE[period] = {
        "period": period, "reference_quarter": ref_q, "url": best[1],
        "filename": best[2], "variant": edition_variant(best[2]),
        "release_page_url": "https://www.gov.uk" + doc["base_path"]}
    return _EDITION_CACHE[period]


def fetch(edition):
    RAW.mkdir(parents=True, exist_ok=True)
    path = RAW / f"{edition['period']}_{edition['filename']}"
    if not path.exists():
        req = urllib.request.Request(edition["url"], headers=UA)
        with urllib.request.urlopen(req, timeout=180) as fh:
            path.write_bytes(fh.read())
    return path


def read_a3(path):
    """Parse sheet A3. Cached per process - the verification suite reads every
    quarter three times, and an ODS parse costs about fifteen seconds."""
    key = str(path)
    if key in _A3_CACHE:
        return _A3_CACHE[key]
    engine = "odf" if path.suffix.lower() == ".ods" else "openpyxl"
    try:
        df = pd.read_excel(path, sheet_name="A3", header=None, engine=engine)
    except ValueError:
        halt(f"{path.name}: no sheet named A3")
    _A3_CACHE[key] = df
    return df


def la_rows(df):
    """Index the English local authority rows by publisher code.

    E92 (England) and E12 (region) rows are deliberately excluded: they are
    weighted estimates that include imputed figures for non-submitting
    authorities, so they are not the sum of the LA rows and must not be loaded
    as though they were another area.
    """
    out = {}
    for i in range(len(df)):
        code = str(df.iat[i, 0]).strip()
        if len(code) == 9 and code[0] == "E" and code[1:3] in ("06", "07", "08", "09"):
            out[code] = i
    return out


def map_columns(df):
    """Match every data column to exactly one canonical category, or stop.

    This is the control that S1 lacked. A positional reader silently produces
    a column of numbers under the wrong name; this one has to account for
    every populated column in the sheet and every category it expects to find,
    and reports what it could not place rather than guessing.
    """
    layout = "v2026_34col" if df.shape[1] == 34 else "legacy_37col"
    header_rows = range(0, 6)
    labels, display = {}, {}
    for j in range(df.shape[1]):
        parts = []
        for i in header_rows:
            cell = df.iat[i, j]
            if pd.notna(cell) and str(cell).strip():
                parts.append(str(cell).strip())
        display[j] = " / ".join(parts)
        labels[j] = " ".join(norm(p) for p in parts)

    populated = set()
    for code, i in la_rows(df).items():
        for j in range(2, df.shape[1]):
            if pd.notna(df.iat[i, j]) and str(df.iat[i, j]).strip():
                populated.add(j)

    patterns = SUPPORT_NEEDS + BREAKDOWN + TOTALS
    mapping, claims = {}, {}
    for code, pattern in patterns:
        hits = [j for j in populated if re.search(pattern, labels[j])]
        if len(hits) > 1:
            halt(f"category '{code}' matched {len(hits)} columns "
                 f"{[display[j] for j in hits]}")
        if hits:
            mapping[hits[0]] = code
            claims.setdefault(code, hits[0])

    anchor = claims.get("hh_one_or_more_support_needs")
    if anchor is None:
        halt("could not locate the 'one or more support needs' column")
    for offset, (code, legacy_text, v2026_pattern) in enumerate(COUNT_BUCKETS, start=1):
        if layout == "v2026_34col":
            hits = [j for j in populated if re.search(v2026_pattern, labels[j])]
            if len(hits) != 1:
                halt(f"'{code}' matched {len(hits)} columns in {layout}")
            col = hits[0]
        else:
            col = anchor + offset
            if col not in populated:
                halt(f"'{code}' expected at column {col} but it is empty")
            if legacy_text not in display[col].split(" / "):
                halt(f"'{code}' expected header '{legacy_text}' at column "
                     f"{col}, found {display[col]!r}")
        if col in mapping:
            halt(f"'{code}' collides with '{mapping[col]}' at column {col}")
        mapping[col] = code

    unmapped = sorted(populated - set(mapping))
    if unmapped:
        halt(f"{layout}: {len(unmapped)} populated column(s) match no known "
             f"category: " + "; ".join(f"[{j}] {display[j]!r}" for j in unmapped))

    missing = [c for c, _ in SUPPORT_NEEDS if c not in mapping.values()]
    if missing:
        halt(f"{layout}: expected support-need categories not found: {missing}")
    for code, _ in BREAKDOWN:
        if code not in mapping.values():
            halt(f"{layout}: breakdown column '{code}' not found")
    if "total_support_needs_count" not in mapping.values():
        halt(f"{layout}: the support-needs count total was not found")
    return layout, mapping, display


def cell(raw):
    """Return (value, flag). Exactly one is non-NULL, enforced in the schema."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None, "missing"
    text = str(raw).strip()
    if text in FLAGS:
        return None, FLAGS[text]
    if text == "":
        return None, "missing"
    try:
        return int(round(float(text))), None
    except ValueError:
        halt(f"unrecognised cell value {text!r} - it is neither a number nor "
             f"a documented marker, and guessing would defeat the point")


def resolve_lookup(cur, codes):
    """Resolve publisher codes to current LAD24 codes via la_code_lookup."""
    cur.execute("SELECT old_code, new_code FROM la_code_lookup "
                "WHERE old_code = ANY(%s)", (list(codes),))
    found = dict(cur.fetchall())
    unresolved = sorted(set(codes) - set(found))
    return found, unresolved


def build_rows(cur, edition, df):
    layout, mapping, display = map_columns(df)
    rows_by_code = la_rows(df)
    resolved, unresolved = resolve_lookup(cur, rows_by_code)
    if unresolved:
        halt(f"{edition['period']}: {len(unresolved)} publisher code(s) do not "
             f"resolve through la_code_lookup: {unresolved}")

    out = []
    for pub_code, i in sorted(rows_by_code.items()):
        lad24cd = resolved[pub_code]
        for j, category in sorted(mapping.items()):
            value, flag = cell(df.iat[i, j])
            out.append((lad24cd, edition["period"], category, value, flag,
                        GROUP[category], display[j], edition["reference_quarter"],
                        edition["url"], edition["filename"], edition["variant"],
                        edition["release_page_url"], layout, pub_code))
    return layout, mapping, out


def log_run(cur, rows_written, periods):
    cur.execute("""
        INSERT INTO pipeline_run_log
            (run_id, source_number, source_code, agent_name, status,
             rows_written, notes, started_at, completed_at)
        VALUES (gen_random_uuid(), %s, %s, %s, 'success', %s, %s,
                now(), now())
    """, ("1b", SOURCE_CODE, "Source 1b - MHCLG homelessness support needs (A3)",
          rows_written,
          f"Loaded {rows_written} rows across {len(periods)} quarters: "
          f"{', '.join(periods)}. Table {TABLE}."))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--discover", action="store_true",
                    help="resolve editions and report A3 structure, write nothing")
    ap.add_argument("--load", action="store_true")
    ap.add_argument("--period", action="append",
                    help="restrict to one or more periods (default: all)")
    args = ap.parse_args()
    if not (args.discover or args.load):
        ap.error("choose --discover or --load")

    periods = args.period or sorted(RELEASES)
    for p in periods:
        if p not in RELEASES:
            halt(f"unknown period {p!r}; known: {sorted(RELEASES)}")

    conn = get_conn()
    cur = conn.cursor()
    try:
        if args.load:
            cur.execute(DDL)
            cur.execute(DDL_GUARDS)

        total = 0
        for period in periods:
            edition = resolve_edition(period)
            df = read_a3(fetch(edition))
            layout, mapping, rows = build_rows(cur, edition, df)
            flagged = sum(1 for r in rows if r[4])
            print(f"{period}  {layout:<12} cols={len(mapping):<3} "
                  f"rows={len(rows):<6} flagged={flagged:<5} "
                  f"{edition['variant']:<9} {edition['filename']}")
            if args.load:
                psycopg2.extras.execute_values(cur, UPSERT, rows, page_size=1000)
            total += len(rows)

        if args.load:
            log_run(cur, total, periods)
            cur.execute(f"SELECT COUNT(*) FROM {TABLE}")
            print(f"\n{TABLE}: {cur.fetchone()[0]} rows ({total} written this run)")
            conn.commit()
            print("COMMITTED")
        else:
            print(f"\n{total} rows would be written. Nothing committed.")
            conn.rollback()
        return 0
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
