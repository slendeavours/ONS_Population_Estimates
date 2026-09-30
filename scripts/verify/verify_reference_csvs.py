"""Check the data/reference CSVs against the original published returns.

Read-only. Prints PASS or FAIL per check and exits 1 if any check fails. Not
part of the verification suite; run by hand after touching data/reference/.

Why it exists: on 2026-09-30 five reference CSVs were found to disagree with
their originals (rough sleeping years shifted by four, support-need columns
read four columns out, suppressed cells written as 0, one RO4 column from the
wrong line). The database and map were already right; these copies were not.

Originals are looked up in the repo first, then in ~/Downloads. A check whose
original is missing is skipped and reported as SKIP.

Barnsley and Sheffield: the rough sleeping and statutory homelessness returns
publish E08000038 and E08000039; the pipeline's canonical codes are E08000016
and E08000019. Codes are normalised to canonical before comparing.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
REF = REPO / "data" / "reference"
RAW = REPO / "data" / "raw" / "s1b_a3"
SRC = REPO / "scripts" / "verify" / "src"
DL = Path.home() / "Downloads"
CANON = {"E08000038": "E08000016", "E08000039": "E08000019"}
CODE = r"^E0[6789]\d{6}$"


def find(*names):
    for base in (RAW, SRC, DL):
        for n in names:
            if (base / n).exists():
                return base / n
    return None


def canon(s):
    return s.replace(CANON)


results = []


def report(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}".rstrip())


def skip(name, why):
    print(f"SKIP  {name}  ({why})")


def compare(name, csv, src, tol=0):
    """csv and src are Series indexed by canonical code. Suppressed = NaN both sides."""
    j = pd.concat([csv.rename("csv"), src.rename("src")], axis=1, join="outer")
    bad = j[~((j.csv.isna() & j.src.isna()) | (j.csv - j.src).abs().le(tol))]
    report(name, bad.empty, f"{len(j)} rows" if bad.empty else f"{len(bad)} differ, e.g. {bad.index[:3].tolist()}")


# --- rough sleeping: 2024 and 2025 columns from Table_1_Total
p = find("Rough_sleeping_snapshot_in_England__autumn_2025.ods")
if p:
    t = pd.read_excel(p, sheet_name="Table_1_Total", engine="odf", header=4)
    t.columns = [str(c).replace(".0", "") for c in t.columns]
    t = t[t["Local Authority Code"].astype(str).str.match(CODE)]
    t = t.set_index(canon(t["Local Authority Code"]))
    c = pd.read_csv(REF / "rough_sleeping_snapshot_2025.csv")
    c = c.set_index(canon(c.lad24cd))
    for col, yr in [("rough_sleeping_2025", "2025"), ("rough_sleeping_2024", "2024")]:
        compare(f"rough sleeping {yr}", c[col], pd.to_numeric(t[yr]))
else:
    skip("rough sleeping", "original .ods not found")

# --- statutory homelessness: header positions in the published tables (A1, TA1, A3)
SH_MAP = {
    "total_assessments": ("A1", 4), "owed_duty": ("A1", 6), "prevention_duty": ("A1", 7),
    "relief_duty": ("A1", 9), "households_in_ta": ("TA1", 4), "support_needs_total": ("A3", 7),
    "mental_health": ("A3", 21), "learning_disability": ("A3", 22), "drug_dependency": ("A3", 26),
    "alcohol_dependency": ("A3", 27), "rough_sleeping_history": ("A3", 30),
}
for quarter, patterns in [("2025Q2", ["*202509*.ods"]), ("2025Q3", ["*202512*.ods"])]:
    p = next((f for base in (RAW, DL) for pat in patterns for f in base.glob(pat)), None)
    if not p:
        skip(f"statutory homelessness {quarter}", "original .ods not found")
        continue
    c = pd.read_csv(REF / f"statutory_homelessness_{quarter}.csv")
    c = c.set_index(canon(c.lad24cd))
    sheets = {}
    for col, (sh, i) in SH_MAP.items():
        if sh not in sheets:
            d = pd.read_excel(p, sheet_name=sh, engine="odf", header=None)
            d = d[d[0].astype(str).str.match(CODE)]
            sheets[sh] = d.set_index(canon(d[0])).apply(pd.to_numeric, errors="coerce")
        compare(f"statutory homelessness {quarter} {col}", c[col], sheets[sh][i])

# --- RO4: total homelessness gross (col 100) and HRA net (col 90); aggregates are skipped
p = find("RO4_LA_Data_2024-25_data_by_LA.ods", "ro4_2024_25.ods")
if p:
    d = pd.read_excel(p, sheet_name="RO4_LA_Data_202425", engine="odf", header=None).iloc[7:]
    d = d[d[1].astype(str).str.match(CODE)]
    d = d.set_index(canon(d[1])).apply(pd.to_numeric, errors="coerce")
    c = pd.read_csv(REF / "ro4_housing_expenditure_2024-25.csv")
    c = c[c.lad24cd.str.match(CODE)]
    c = c.set_index(canon(c.lad24cd))
    compare("RO4 total homelessness gross", c.total_homelessness_gross_exp_000, d[100])
    compare("RO4 HRA net", c.hra_admin_prevention_relief_net_exp_000, d[90])
else:
    skip("RO4", "original .ods not found")

# --- LAHS waiting list, 2025 only (older years carry predecessor-authority duplicates)
p = find("lahs_2024_25.ods")
if p:
    l = pd.read_excel(p, sheet_name="Local_Authority_Data", engine="odf", header=2)
    l = l[l["local_authority_code"].astype(str).str.match(CODE)]
    l = l.set_index(canon(l["local_authority_code"]))
    c = pd.read_csv(REF / "lahs_waiting_list_2015_2025.csv")
    c = c[c.reporting_year == 2025]
    c = c.set_index(canon(c.lad24cd))
    compare("LAHS 2025 households on register", c.households_on_register, pd.to_numeric(l["cc1a"], errors="coerce"))
else:
    skip("LAHS", "original .ods not found")

# --- IMD
p = find("imd2025_file10.xlsx")
if p:
    i = pd.read_excel(p, sheet_name="IMD")
    i = i.set_index(canon(i.iloc[:, 0]))
    c = pd.read_csv(REF / "imd_2025_la_summary.csv")
    c = c.set_index(canon(c.lad24cd))
    cols = [x for x in c.columns if x.startswith("imd_")]
    for k, s in zip(cols, i.columns[2:]):
        compare(f"IMD {k}", c[k], i[s], tol=1e-9)
else:
    skip("IMD", "original .xlsx not found")

print(f"\n{sum(results)} of {len(results)} checks passed")
sys.exit(0 if all(results) else 1)
