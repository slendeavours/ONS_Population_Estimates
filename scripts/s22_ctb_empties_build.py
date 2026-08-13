"""S22 — MHCLG Council Taxbase empty homes.

Builds four tables and one view in exempt_pipeline from two MHCLG publishers:

  A  Local authority Council Taxbase in England (annual, published November,
     revised the following January)
  B  Table 615, vacant dwellings by local authority district, from 2004

Neither file URL is hardcoded. Both are resolved at runtime from their
publisher landing pages by scripts/s22_ctb_discover.py.

Geography is stored as published. lad24cd resolution goes through
la_code_lookup; nothing is written back to it. Derived rates live in
v_la_empty_homes_rates, never in a table.

Phases:
  discover   Phase 1 — resolve, download, write the structure report
  load       Phase 2/3 — create tables, load, create the view, run the
             hard gates inside one transaction and roll back on any failure
  verify     Phase 6 — full verification suite to build_reports/

Run `python scripts/s22_ctb_empties_build.py all` for the whole sequence.
"""
import datetime
import json
import re
import sys
from pathlib import Path

import openpyxl
import pandas as pd
import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _db import get_conn  # noqa: E402
import s22_ctb_discover as disco  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
REPORT_DIR = REPO / "build_reports"
STATE_PATH = REPORT_DIR / "s22_build_state.json"

AGENT_NAME = "Source 22 - MHCLG Council Taxbase Empty Homes"

# Table 2.01 exemption classes that describe an UNOCCUPIED dwelling. This set
# is not assumed: summed across England it reproduces the release page's
# "212,000 dwellings that were receiving an exemption that were unoccupied".
UNOCCUPIED_CLASSES = ["B", "D", "E", "F", "G", "H", "I", "J", "K", "L", "Q"]

CLASS_DESCRIPTIONS = {
    "B": "Unoccupied dwelling owned by a charity (up to six months)",
    "D": "Unoccupied dwelling left empty by a person detained in prison or "
         "hospital",
    "E": "Unoccupied dwelling previously the sole residence of a person now "
         "in a care home or hospital",
    "F": "Unoccupied dwelling where the liable person is deceased "
         "(or probate granted less than six months ago)",
    "G": "Unoccupied dwelling whose occupation is prohibited by law",
    "H": "Unoccupied dwelling held for a minister of religion",
    "I": "Unoccupied dwelling left empty by a person receiving personal care "
         "elsewhere",
    "J": "Unoccupied dwelling left empty by a person providing personal care "
         "elsewhere",
    "K": "Unoccupied dwelling owned by a student and last occupied by "
         "students",
    "L": "Unoccupied dwelling where a mortgagee is in possession",
    "Q": "Unoccupied dwelling left empty by a bankrupt person's trustee",
}

# ── Phase 1: discovery ──────────────────────────────────────────────────────


def phase0_source_number(conn):
    """Next free integer source_number above the highest already used."""
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT source_number FROM pipeline_run_log")
    used_raw = [r[0] for r in cur.fetchall()]
    used_int = set()
    for v in used_raw:
        m = re.match(r"^(?:s)?(\d+)", str(v).strip(), re.I)
        if m:
            used_int.add(int(m.group(1)))
    n = max(used_int) + 1
    while n in used_int:
        n += 1
    return n, sorted(used_int), used_raw


# ── Extraction: Source A ────────────────────────────────────────────────────

def _block_total_col(label_row, header_row, table_number):
    """0-indexed column of the `Total` header inside a named table block."""
    starts = sorted(i for i, v in enumerate(label_row) if v is not None)
    pattern = re.compile(r"Table\s+" + re.escape(table_number) + r"[.\s]")
    for k, s in enumerate(starts):
        if pattern.search(str(label_row[s])):
            end = starts[k + 1] if k + 1 < len(starts) else len(label_row)
            for i in range(s, end):
                if str(header_row[i]).strip() == "Total":
                    return i, str(label_row[s]).strip()
            disco.halt(f"Table {table_number} block found but it has no "
                       "'Total' column — structure has changed")
    disco.halt(f"Table {table_number} not found on the sheet — structure has "
               "changed")


def _sheet_rows(path, sheet):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[sheet]
    rows = list(ws.iter_rows(min_row=5, values_only=True))
    wb.close()
    return rows[0], rows[1], rows[2:]


def resolve_recodes(conn):
    """Pure recodes from la_code_lookup: published code -> current lad24cd.

    A recode renumbers an area without changing its boundary, so it resolves.
    Abolitions into successor unitaries are deliberately not included here;
    see resolve_615_geography.
    """
    cur = conn.cursor()
    cur.execute("SELECT lad24cd FROM la_boundaries")
    current = {r[0] for r in cur.fetchall()}
    cur.execute("SELECT old_code, new_code FROM la_code_lookup "
                "WHERE change_type = 'recode'")
    return current, {r[0]: r[1] for r in cur.fetchall() if r[1] in current}


def extract_council_taxbase(src_a, conn=None):
    """One record per billing authority, plus the exemption class long rows."""
    path = src_a["path"]
    year = src_a["taxbase_year"]

    label, header, data = _sheet_rows(path, "Council Taxbase Data")
    wanted = {
        "total_dwellings": "1.01",
        "second_homes": "1.11",
        "empty_homes_premium_count": "1.17",
        "empty_total": "1.18",
        "empty_6_months_plus": "1.19",
    }
    cols, table_meta = {}, []
    for field, tno in wanted.items():
        c, title = _block_total_col(label, header, tno)
        cols[field] = c
        table_meta.append({"table": tno, "header": "Total", "col": c,
                           "title": title, "field": field})

    slabel, sheader, sdata = _sheet_rows(path, "Supplementary Data")
    class_cols = {}
    starts = sorted(i for i, v in enumerate(slabel) if v is not None)
    s201 = next((s for s in starts
                 if re.search(r"Table\s+2\.01[.\s]", str(slabel[s]))), None)
    if s201 is None:
        disco.halt("Table 2.01 not found on the Supplementary Data sheet")
    end201 = starts[starts.index(s201) + 1]
    for i in range(s201, end201):
        m = re.match(r"Class ([A-W])\b", str(sheader[i]).strip())
        if m and "not in use" not in str(sheader[i]):
            class_cols[m.group(1)] = i
    missing = [c for c in UNOCCUPIED_CLASSES if c not in class_cols]
    if missing:
        disco.halt(f"Table 2.01 is missing exemption classes {missing} — "
                   "LA-level class breakdown structure has changed")
    table_meta.append({"table": "2.01", "header": "Class B ... Class W",
                       "col": f"{s201}-{end201 - 1}",
                       "title": str(slabel[s201]).strip(),
                       "field": "exemption classes"})

    supp_by_code = {str(r[1]).strip(): r for r in sdata
                    if r[1] and str(r[1]).startswith("E0")}

    publication = (f"{src_a['release_title']} — {src_a['attachment_title']}; "
                   f"first published "
                   f"{(src_a['first_published'] or '')[:10]}, revised "
                   f"{(src_a['public_updated'] or '')[:10]}")

    records, class_rows = [], []
    england = None
    for r in data:
        code = str(r[1]).strip() if r[1] else ""
        name = r[3]
        if code == "E92000001":
            england = {f: r[c] for f, c in cols.items()}
            england["empty_under_6_months"] = (r[cols["empty_total"]]
                                               - r[cols["empty_6_months_plus"]])
            sr = supp_by_code.get(code) or next(
                (x for x in sdata if str(x[1]).strip() == code), None)
            england["unoccupied_exemptions_total"] = sum(
                sr[class_cols[c]] or 0 for c in UNOCCUPIED_CLASSES)
            continue
        if not code.startswith("E0"):
            continue

        vals = {}
        for f, c in cols.items():
            v = r[c]
            vals[f] = int(v) if isinstance(v, (int, float)) else None
        if vals["empty_total"] is None or vals["empty_6_months_plus"] is None:
            vals["empty_under_6_months"] = None
        else:
            vals["empty_under_6_months"] = (vals["empty_total"]
                                            - vals["empty_6_months_plus"])

        sr = supp_by_code.get(code)
        if sr is None:
            disco.halt(f"{code} present on Council Taxbase Data but absent "
                       "from Supplementary Data")
        unocc = 0
        for cl in UNOCCUPIED_CLASSES:
            v = sr[class_cols[cl]]
            unocc += int(v) if isinstance(v, (int, float)) else 0
            class_rows.append({
                "lad24cd": code, "taxbase_year": year, "exemption_class": cl,
                "exemption_description": CLASS_DESCRIPTIONS[cl],
                "dwellings": int(v) if isinstance(v, (int, float)) else None,
            })
        vals["unoccupied_exemptions_total"] = unocc

        records.append(dict(
            lad24cd=code, la_name=str(name).strip(), taxbase_year=year,
            source_publication=publication, **vals))

    if england is None:
        disco.halt("England total row (E92000001) not found in the workbook")

    # MHCLG publishes Barnsley and Sheffield under the codes recoded on
    # 1 April 2025 (SI 1328/2024); la_boundaries is LAD Dec 2024 and still
    # carries the pre-recode codes. la_code_lookup holds both as change_type
    # 'recode' — same area, new number — so they resolve rather than being
    # left unmapped. Recorded so the substitution is visible, never silent.
    recodes_applied = []
    if conn is not None:
        current, recodes = resolve_recodes(conn)
        for rec in records:
            new = recodes.get(rec["lad24cd"])
            if new:
                recodes_applied.append({"published": rec["lad24cd"],
                                        "lad24cd": new,
                                        "la_name": rec["la_name"]})
                rec["lad24cd"] = new
        for row in class_rows:
            new = recodes.get(row["lad24cd"])
            if new:
                row["lad24cd"] = new
        seen = {}
        for rec in records:
            if rec["lad24cd"] in seen:
                disco.halt(f"recode collision: {rec['lad24cd']} claimed by "
                           f"both {seen[rec['lad24cd']]} and "
                           f"{rec['la_name']} — resolve before loading")
            seen[rec["lad24cd"]] = rec["la_name"]
        unresolved = [(r["lad24cd"], r["la_name"]) for r in records
                      if r["lad24cd"] not in current]
        if unresolved:
            disco.halt(
                "UNEXPLAINED codes not present in la_boundaries and not held "
                f"as a recode in la_code_lookup: {unresolved}. An unresolved "
                "code is a hard stop; establish what it is against an "
                "authoritative source before loading.")

    return (records, class_rows, england, table_meta, publication,
            recodes_applied)


# ── Extraction: Source B ────────────────────────────────────────────────────

def _year_from_header(v):
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.year
    s = str(v).strip()
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", s)
    if m:
        return int(m.group(3))
    m = re.match(r"^(\d{4})", s)
    return int(m.group(1)) if m else None


def _num(v):
    if v is None:
        return None
    if isinstance(v, str):
        if v.strip() in ("[x]", "[c]", "[z]", "[w]", ":", "-", ""):
            return None
        try:
            return int(round(float(v.replace(",", ""))))
        except ValueError:
            return None
    if isinstance(v, float) and pd.isna(v):
        return None
    if isinstance(v, (int, float)):
        return int(round(v))
    return None


def extract_table_615(src_b):
    frames = {}
    for sheet, field in (("All_vacants", "vacant_dwellings"),
                         ("All_long_term_vacants",
                          "long_term_vacant_dwellings")):
        df = pd.read_excel(src_b["path"], sheet_name=sheet, engine="odf",
                           header=None)
        header = df.iloc[2].tolist()
        years = {i: _year_from_header(header[i])
                 for i in range(2, len(header))}
        if not any(years.values()):
            disco.halt(f"Table 615 sheet {sheet}: no year headers parsed — "
                       "structure has changed")
        rec = {}
        for _, row in df.iloc[3:].iterrows():
            code = str(row[0]).strip()
            if not re.match(r"^E0[6789]\d{6}$", code):
                continue
            name = str(row[1]).strip()
            for i, yr in years.items():
                if yr is None:
                    continue
                v = _num(row[i])
                if v is None:
                    continue
                rec[(code, yr)] = (name, v)
        frames[field] = rec

    keys = sorted(set(frames["vacant_dwellings"])
                  | set(frames["long_term_vacant_dwellings"]))
    out = []
    for code, yr in keys:
        a = frames["vacant_dwellings"].get((code, yr))
        b = frames["long_term_vacant_dwellings"].get((code, yr))
        name = (a or b)[0]
        out.append({"published_la_code": code, "published_la_name": name,
                    "year": yr,
                    "vacant_dwellings": a[1] if a else None,
                    "long_term_vacant_dwellings": b[1] if b else None})
    return out


def resolve_615_geography(conn, rows):
    """direct | resolved_via_lookup | unmapped, via la_code_lookup only.

    A pure recode (same area, new code) resolves. An abolition into a
    successor unitary does not: mapping five Somerset districts onto
    E06000066 would make any downstream sum count Somerset six times. Those
    stay unmapped with a null lad24cd, as does anything the lookup does not
    hold.
    """
    cur = conn.cursor()
    cur.execute("SELECT lad24cd FROM la_boundaries")
    current = {r[0] for r in cur.fetchall()}
    cur.execute("SELECT old_code, new_code, change_type FROM la_code_lookup")
    lookup = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

    for r in rows:
        code = r["published_la_code"]
        if code in current:
            r["lad24cd"], r["mapping_status"] = code, "direct"
            continue
        hit = lookup.get(code)
        if hit and hit[1] == "recode" and hit[0] in current:
            r["lad24cd"], r["mapping_status"] = hit[0], "resolved_via_lookup"
            continue
        r["lad24cd"], r["mapping_status"] = None, "unmapped"
    return rows


# ── DDL ─────────────────────────────────────────────────────────────────────

DDL = """
CREATE TABLE IF NOT EXISTS la_council_taxbase_empties (
    lad24cd                     VARCHAR(9)  NOT NULL,
    la_name                     VARCHAR(100),
    taxbase_year                INTEGER     NOT NULL,
    total_dwellings             INTEGER,
    empty_under_6_months        INTEGER,
    empty_6_months_plus         INTEGER,
    empty_total                 INTEGER,
    empty_homes_premium_count   INTEGER,
    second_homes                INTEGER,
    unoccupied_exemptions_total INTEGER,
    source_publication          TEXT,
    loaded_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (lad24cd, taxbase_year)
);

CREATE TABLE IF NOT EXISTS la_ctb_exemption_classes (
    lad24cd               VARCHAR(9)  NOT NULL,
    taxbase_year          INTEGER     NOT NULL,
    exemption_class       VARCHAR(2)  NOT NULL,
    exemption_description TEXT,
    dwellings             INTEGER,
    loaded_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (lad24cd, taxbase_year, exemption_class)
);

CREATE TABLE IF NOT EXISTS la_vacant_dwellings_615 (
    published_la_code          VARCHAR(9)  NOT NULL,
    published_la_name          VARCHAR(100),
    year                       INTEGER     NOT NULL,
    vacant_dwellings           INTEGER,
    long_term_vacant_dwellings INTEGER,
    lad24cd                    VARCHAR(9),
    mapping_status             VARCHAR(20) NOT NULL,
    loaded_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (published_la_code, year)
);

CREATE TABLE IF NOT EXISTS ctb_series_breaks (
    break_id        SERIAL PRIMARY KEY,
    first_period    DATE NOT NULL,
    last_period     DATE,
    affected_column TEXT NOT NULL,
    dimension       TEXT,
    description     TEXT,
    comparability   TEXT,
    source_url      TEXT
);

CREATE INDEX IF NOT EXISTS ix_ctb_empties_year
    ON la_council_taxbase_empties (taxbase_year);
CREATE INDEX IF NOT EXISTS ix_ctb_615_lad24cd
    ON la_vacant_dwellings_615 (lad24cd);
"""

VIEW_SQL = """
DROP VIEW IF EXISTS v_la_empty_homes_rates;
CREATE VIEW v_la_empty_homes_rates AS
SELECT
    e.lad24cd,
    e.la_name,
    e.taxbase_year,
    e.total_dwellings,
    e.empty_total,
    e.empty_6_months_plus,
    e.empty_homes_premium_count,
    e.second_homes,
    ROUND(e.empty_6_months_plus::NUMERIC
          / NULLIF(e.total_dwellings, 0) * 100, 2)      AS lte_rate_pct,
    ROUND(e.empty_6_months_plus::NUMERIC
          / NULLIF(e.empty_total, 0) * 100, 2)          AS lte_share_of_empties_pct,
    ROUND(e.empty_homes_premium_count::NUMERIC
          / NULLIF(e.empty_6_months_plus, 0) * 100, 2)  AS premium_coverage_pct,
    ROUND(e.second_homes::NUMERIC
          / NULLIF(e.total_dwellings, 0) * 100, 2)      AS second_homes_rate_pct
FROM la_council_taxbase_empties e
WHERE e.taxbase_year = (SELECT MAX(taxbase_year)
                          FROM la_council_taxbase_empties);

COMMENT ON VIEW v_la_empty_homes_rates IS
 'Derived empty homes rates over the latest taxbase year in '
 'la_council_taxbase_empties. Rates are computed here and never stored.';
COMMENT ON COLUMN v_la_empty_homes_rates.lte_rate_pct IS
 'Long-term empty (6 months or more) as a percentage of all dwellings on the '
 'valuation list.';
COMMENT ON COLUMN v_la_empty_homes_rates.lte_share_of_empties_pct IS
 'Long-term empty as a percentage of all dwellings classed as empty.';
COMMENT ON COLUMN v_la_empty_homes_rates.premium_coverage_pct IS
 'Directional only. This can never reach 100: long-term empty starts at six '
 'months while the Empty Homes Premium starts at twelve, so the numerator is '
 'drawn from a strictly narrower population than the denominator. It '
 'indicates how far an authority applies the premium across its long-term '
 'empty stock, and is not a compliance rate.';
COMMENT ON COLUMN v_la_empty_homes_rates.second_homes_rate_pct IS
 'Second homes as a percentage of all dwellings on the valuation list.';
"""

SERIES_BREAKS = [
    dict(first_period="2024-04-01", last_period=None,
         affected_column="empty_homes_premium_count",
         dimension="premium threshold",
         description=(
             "From 1 April 2024 authorities could charge an Empty Homes "
             "Premium of up to 100% on properties empty for between 1 and 2 "
             "years. Previously the premium could only be applied where a "
             "property had been empty for 2 or more years."),
         comparability=(
             "empty_homes_premium_count is not comparable before and after 1 "
             "April 2024. The eligible population widened; a rise across this "
             "date is a threshold change, not more empty homes. England "
             "premium counts rose 27.9% between the 2024 and 2025 taxbase "
             "years.")),
    dict(first_period="2025-04-01", last_period=None,
         affected_column="second_homes",
         dimension="premium introduction",
         description=(
             "From 1 April 2025 authorities could charge a Second Homes "
             "Premium of up to 100% on properties reported as second homes "
             "for council tax purposes. In the 2025 taxbase year 211 of 296 "
             "authorities applied it."),
         comparability=(
             "second_homes is affected by reclassification behaviour from 1 "
             "April 2025. Authorities reported reviewing empty properties and "
             "second homes ahead of the new premium, which moves dwellings "
             "between the empty and second home categories independently of "
             "any change on the ground.")),
]


# ── Load ────────────────────────────────────────────────────────────────────

def load_all(conn, records, class_rows, rows615, tech_notes_url):
    cur = conn.cursor()
    cur.execute(DDL)

    psycopg2.extras.execute_batch(cur, """
        INSERT INTO la_council_taxbase_empties (
            lad24cd, la_name, taxbase_year, total_dwellings,
            empty_under_6_months, empty_6_months_plus, empty_total,
            empty_homes_premium_count, second_homes,
            unoccupied_exemptions_total, source_publication, loaded_at)
        VALUES (%(lad24cd)s, %(la_name)s, %(taxbase_year)s,
                %(total_dwellings)s, %(empty_under_6_months)s,
                %(empty_6_months_plus)s, %(empty_total)s,
                %(empty_homes_premium_count)s, %(second_homes)s,
                %(unoccupied_exemptions_total)s, %(source_publication)s, now())
        ON CONFLICT (lad24cd, taxbase_year) DO UPDATE SET
            la_name                     = EXCLUDED.la_name,
            total_dwellings             = EXCLUDED.total_dwellings,
            empty_under_6_months        = EXCLUDED.empty_under_6_months,
            empty_6_months_plus         = EXCLUDED.empty_6_months_plus,
            empty_total                 = EXCLUDED.empty_total,
            empty_homes_premium_count   = EXCLUDED.empty_homes_premium_count,
            second_homes                = EXCLUDED.second_homes,
            unoccupied_exemptions_total = EXCLUDED.unoccupied_exemptions_total,
            source_publication          = EXCLUDED.source_publication,
            loaded_at                   = now()
    """, records, page_size=200)

    psycopg2.extras.execute_batch(cur, """
        INSERT INTO la_ctb_exemption_classes (
            lad24cd, taxbase_year, exemption_class, exemption_description,
            dwellings, loaded_at)
        VALUES (%(lad24cd)s, %(taxbase_year)s, %(exemption_class)s,
                %(exemption_description)s, %(dwellings)s, now())
        ON CONFLICT (lad24cd, taxbase_year, exemption_class) DO UPDATE SET
            exemption_description = EXCLUDED.exemption_description,
            dwellings             = EXCLUDED.dwellings,
            loaded_at             = now()
    """, class_rows, page_size=500)

    psycopg2.extras.execute_batch(cur, """
        INSERT INTO la_vacant_dwellings_615 (
            published_la_code, published_la_name, year, vacant_dwellings,
            long_term_vacant_dwellings, lad24cd, mapping_status, loaded_at)
        VALUES (%(published_la_code)s, %(published_la_name)s, %(year)s,
                %(vacant_dwellings)s, %(long_term_vacant_dwellings)s,
                %(lad24cd)s, %(mapping_status)s, now())
        ON CONFLICT (published_la_code, year) DO UPDATE SET
            published_la_name          = EXCLUDED.published_la_name,
            vacant_dwellings           = EXCLUDED.vacant_dwellings,
            long_term_vacant_dwellings = EXCLUDED.long_term_vacant_dwellings,
            lad24cd                    = EXCLUDED.lad24cd,
            mapping_status             = EXCLUDED.mapping_status,
            loaded_at                  = now()
    """, rows615, page_size=500)

    for b in SERIES_BREAKS:
        cur.execute("""
            INSERT INTO ctb_series_breaks (
                first_period, last_period, affected_column, dimension,
                description, comparability, source_url)
            SELECT %(first_period)s, %(last_period)s, %(affected_column)s,
                   %(dimension)s, %(description)s, %(comparability)s, %(url)s
             WHERE NOT EXISTS (
                SELECT 1 FROM ctb_series_breaks
                 WHERE first_period = %(first_period)s
                   AND affected_column = %(affected_column)s)
        """, dict(b, url=tech_notes_url))

    cur.execute(VIEW_SQL)
    return cur


def main():
    print("s22_ctb_empties_build: use the phase runner in "
          "scripts/s22_run.py")


if __name__ == "__main__":
    main()
