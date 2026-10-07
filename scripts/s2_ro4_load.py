"""S2 RO4 housing expenditure: header-driven loader, additive by financial year.

RO4 redesigns its worksheet between years (2025-26 has 211 columns against 187
and renames the Homeless Reduction Act line), so every column is resolved by
its header text and the script refuses to continue unless each resolves to
exactly one column. Positions are never used.

Insert only. A (lad24cd, financial_year) already in the table is left as it is;
a later release of the same year needs its own release marker before it can be
held alongside, and this script will not overwrite one. A later release of a year already held goes
through scripts/ro4_editions.py load (append-only editions table), then
ro4_editions.py refresh-latest; this script stays the parser and first-load tool.

    python scripts/s2_ro4_load.py reproduce 2024-25    # parse the file at the FILES name (the 2024-25 one is the SECOND release), compare with the table
    python scripts/s2_ro4_load.py dry-run   2025-26    # parse and report, write nothing
    python scripts/s2_ro4_load.py apply     2025-26    # insert missing rows

Every mode first reads the workbook's own Front_Page and refuses if its release
ordinal and date disagree with the label this script is about to record.
"""
import re
import sys
import warnings
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _db  # noqa: E402

warnings.filterwarnings("ignore")
REF = Path(__file__).resolve().parent.parent / "data" / "reference"

FILES = {"2024-25": ("RO4_LA_Data_2024-25_data_by_LA.ods", "RO4_LA_Data_202425",
                     "MHCLG Revenue Outturn RO4 2024-25, second release, published 4 December 2025"),
         "2025-26": ("RO4_LA_Data_2025-26_data_by_LA.ods", "RO4_LA_Data_202526",
                     "MHCLG Revenue Outturn RO4 2025-26, first release, published 17 Sep 2026")}

TOT, NET = r" - Total Expenditure \(C3", r" - Net Current Expenditure \(C7"
# table column -> (header regex, which period the label belongs to)
COLS = {
    "nightly_paid_ta_gross_exp_000": r"^Nightly paid, privately managed accommodation, self-contained" + TOT,
    "nightly_paid_ta_net_exp_000": r"^Nightly paid, privately managed accommodation, self-contained" + NET,
    "hostels_gross_exp_000": r"^Hostels \(including reception centres, emergency units and refuges\)" + TOT,
    "hostels_net_exp_000": r"^Hostels \(including reception centres, emergency units and refuges\)" + NET,
    "bb_gross_exp_000": r"^Bed and breakfast hotels \(including shared annexes\)" + TOT,
    "bb_net_exp_000": r"^Bed and breakfast hotels \(including shared annexes\)" + NET,
    # The line is 'Homeless Reduction Act: Administration, Prevention, Relief & Support' to 2024-25 and
    # 'Homelessness Services: Administration, Prevention, Relief & Main Duty' from 2025-26.
    "hra_admin_prevention_relief_net_exp_000":
        r"^(?:Homeless Reduction Act|Homelessness Services): Administration, Prevention, Relief[^-]*" + NET,
    "total_homelessness_gross_exp_000": r"^TOTAL HOMELESSNESS" + TOT,
    "total_homelessness_net_exp_000": r"^TOTAL HOMELESSNESS" + NET,
    "total_housing_gross_exp_000": r"^TOTAL HOUSING SERVICES \(GFRA only\)" + TOT,
    "total_housing_net_exp_000": r"^TOTAL HOUSING SERVICES \(GFRA only\)" + NET,
}
# The table keys Barnsley and Sheffield on their pre-2025 codes; later releases publish the new ones.
TO_TABLE_CODE = {"E08000038": "E08000016", "E08000039": "E08000019"}


FRONT_PAGE = re.compile(r"(\w+) release\W+which was published on "
                        r"(\d{1,2} [A-Za-z]+ \d{4})", re.I)


def front_page_release(path) -> set:
    """{(ordinal word, date)} of the release sentences on the workbook's own
    Front_Page sheet, e.g. ('third', date(2026, 6, 11))."""
    fp = pd.read_excel(path, sheet_name="Front_Page", engine="odf", header=None)
    out = set()
    for v in fp.stack().astype(str):
        for hit in FRONT_PAGE.finditer(v):
            out.add((hit.group(1).lower(),
                     datetime.strptime(hit.group(2), "%d %B %Y").date()))
    return out


def _label_date(label):
    text = re.search(r"published (\d{1,2} [A-Za-z]+ \d{4})", label or "")
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(text.group(1), fmt).date()
        except (AttributeError, ValueError):
            pass
    return None


def cover_problems(path, label) -> list:
    """Problems if the workbook's own cover sheet does not say what the release
    label we are about to record says (ordinal word and publication date)."""
    try:
        found = front_page_release(path)
    except Exception as e:  # unreadable workbook or no Front_Page sheet
        return [f"{Path(path).name}: cannot read the Front_Page ({type(e).__name__})"]
    word = re.search(r"(\w+) release", label or "", re.I)
    want = (word.group(1).lower() if word else None, _label_date(label))
    if found != {want}:
        return [f"{Path(path).name}: cover sheet says {sorted(found)} but the "
                f"label to be recorded says {want[0]!r} release published "
                f"{want[1]} ({label!r})"]
    return []


def parse(fy, conn, spec=None):
    """Parse one RO4 workbook. spec (optional) is a manifest entry or any dict
    with 'file' (a name in data/reference, or an absolute path), 'sheet' and
    'source' (the text for the source column); without it the FILES entry of
    the year is used, as before."""
    if spec is None:
        fname, sheet, source = FILES[fy]
    else:
        fname, sheet, source = spec["file"], spec["sheet"], spec["source"]
    problems = cover_problems(REF / fname, source)
    if problems:
        raise SystemExit(f"{fy}: refusing to parse, the file's own cover sheet "
                         "disagrees with its release label: " + "; ".join(problems))
    raw = pd.read_excel(REF / fname, sheet_name=sheet, engine="odf", header=None)
    hdr = [str(v) for v in raw.iloc[6]]
    pos = {}
    for col, rx in COLS.items():
        hit = [i for i, h in enumerate(hdr) if re.search(rx, h)]
        if len(hit) != 1:
            raise SystemExit(f"{fy}: '{col}' matched {len(hit)} headers; refusing to guess: {[hdr[i] for i in hit]}")
        pos[col] = hit[0]
    body = raw.iloc[7:].copy()
    body["lad24cd"] = body[1].astype(str).str.strip().replace(TO_TABLE_CODE)
    valid = set(pd.read_sql("select lad24cd from la_boundaries", conn).lad24cd)
    body = body[body["lad24cd"].isin(valid)]
    out = pd.DataFrame({"lad24cd": body["lad24cd"], "la_name": body[2].astype(str).str.strip()})
    for col, i in pos.items():
        out[col] = pd.to_numeric(body[i], errors="coerce")  # '[x]' and similar suppression markers -> NULL
    out["financial_year"] = fy
    out["data_missing"] = out["total_homelessness_gross_exp_000"].isna()
    out["source"] = source
    dup = out[out.lad24cd.duplicated(keep=False)]
    if len(dup):
        raise SystemExit(f"{fy}: duplicate codes {sorted(set(dup.lad24cd))}")
    return out.reset_index(drop=True), valid, pos, hdr


def main():
    mode, fy = sys.argv[1], sys.argv[2]
    conn = _db.get_conn() if mode == "apply" else _db.get_readonly_conn()
    df, valid, pos, hdr = parse(fy, conn)
    print(f"{fy}: {len(df)} authorities parsed, {int(df.data_missing.sum())} with no homelessness figure "
          f"({', '.join(df[df.data_missing].la_name)})")
    print("  columns resolved by header:")
    for c, i in pos.items():
        print(f"    {c:42s} <- [{i}] {hdr[i][:80]}")
    print("  not in source:", sorted(valid - set(df.lad24cd)))
    if mode == "reproduce":
        db = pd.read_sql("select * from ro4_housing_expenditure where financial_year=%s", conn, params=(fy,)).set_index("lad24cd")
        bad = 0
        for c in COLS:
            if c == "hra_admin_prevention_relief_net_exp_000":
                print(f"  {c}: skipped (only the earlier live rows, 2024-25 edition 1, held TA administration net under this name; the second and third releases have the right line, see docs/decisions/2026-10-07-ro4-edition-history.md)")
                continue
            a = df.set_index("lad24cd")[c].astype(float); b = db[c].astype(float).reindex(a.index)
            n = int(((a.fillna(-1) - b.fillna(-1)).abs() > 0.005).sum()); bad += n
            print(f"  {c}: {n} of {len(a)} differ from the table")
        print("REPRODUCES" if bad == 0 else f"{bad} cell differences, see above")
    else:
        g = df.total_homelessness_gross_exp_000
        print(f"  total homelessness gross: £{g.sum()/1000:,.0f}m across {int(g.notna().sum())} LAs")
        if mode == "apply":
            cur = conn.cursor(); n = 0
            cols = ["lad24cd", "la_name", "financial_year"] + list(COLS) + ["data_missing", "source"]
            sql = (f"insert into ro4_housing_expenditure ({','.join(cols)}) values ({','.join(['%s'] * len(cols))}) "
                   "on conflict (lad24cd, financial_year) do nothing")
            for r in df[cols].astype(object).where(df[cols].notna(), None).itertuples(index=False):
                cur.execute(sql, [None if v is pd.NA else v for v in r]); n += cur.rowcount
            conn.commit()
            print(f"  inserted {n} rows ({len(df) - n} already present and left untouched)")
        else:
            print("  dry run: nothing written")


if __name__ == "__main__":
    main()
