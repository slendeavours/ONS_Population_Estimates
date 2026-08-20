"""Verify the loaded DRD month against publication, then load any newer month."""
import os
import sys
import datetime
import pandas as pd
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv(r"C:\Users\slewi\ucws-repo\.env")
HERE = os.path.dirname(os.path.abspath(__file__))

MEASURES = {
    "Total Discharges for UTLA from acceptable trusts": "total_discharges_acceptable_trusts",
    "% of all UTLA discharges that are from acceptable trusts": "pct_acceptable_trust_coverage",
    "Total bed days lost due to delayed discharge": "total_bed_days_lost",
    "Date of discharge is same as Discharge Ready Date": "pct_same_day_discharge",
    "Date of Discharge is 1+ days after Discharge Ready Date": "pct_delayed_1plus_days",
    "Number of patients discharged where, between the Discharge Ready Date and Discharge Date, there is No delay": "discharged_no_delay",
    "Number of patients discharged where, between the Discharge Ready Date and Discharge Date, there is a 1 day delay": "discharged_1_day",
    "Number of patients discharged where, between the Discharge Ready Date and Discharge Date, there is a 2-3 day delay": "discharged_2_3_days",
    "Number of patients discharged where, between the Discharge Ready Date and Discharge Date, there is a 4-6 day delay": "discharged_4_6_days",
    "Number of patients discharged where, between the Discharge Ready Date and Discharge Date, there is a 7-13 day delay": "discharged_7_13_days",
    "Number of patients discharged where, between the Discharge Ready Date and Discharge Date, there is a 14-20 day delay": "discharged_14_20_days",
    "Number of patients discharged where, between the Discharge Ready Date and Discharge Date, there is a delay of 21 days or more": "discharged_21_plus_days",
    "Average days from Discharge Ready Date to date of discharge (inc 0 day delays)": "avg_days_drd_to_discharge_inc_zero",
    "Average days from Discharge Ready Date to date of discharge (exc 0 day delays)": "avg_days_drd_to_discharge_exc_zero",
}
INTS = {"total_discharges_acceptable_trusts", "total_bed_days_lost",
        "discharged_no_delay", "discharged_1_day", "discharged_2_3_days",
        "discharged_4_6_days", "discharged_7_13_days", "discharged_14_20_days",
        "discharged_21_plus_days"}


def parse(path, period):
    d = pd.read_csv(path, low_memory=False)
    d = d[d["Data Type"].astype(str).str.strip() == "UTLA Aggregate"]
    d["Measure"] = d["Measure"].astype(str).str.strip()
    out = {}
    for _, r in d.iterrows():
        col = MEASURES.get(r["Measure"])
        if not col:
            continue
        code = str(r["Upper Tier Local Authority Code"]).strip()
        if not code.startswith("E"):
            continue
        v = str(r["Value"]).strip()
        if v.startswith("[") or v in ("", "nan"):
            val = None
        else:
            try:
                val = int(float(v)) if col in INTS else float(v)
            except ValueError:
                val = None
        rec = out.setdefault(code, {"utla_code": code,
                                    "utla_name": str(r["Upper Tier Local Authority"]).strip(),
                                    "reporting_period": period})
        rec[col] = val
    return out


conn = psycopg2.connect(host="localhost", port=os.getenv("PG_PORT"),
                        dbname=os.getenv("PG_DATABASE"), user=os.getenv("PG_USER"),
                        password=os.getenv("PG_PASSWORD"))
cur = conn.cursor()
c2 = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

# --- verify May 2026, already loaded -----------------------------------
may = parse(os.path.join(HERE, "src/drd_May-2026.csv"), datetime.date(2026, 5, 1))
c2.execute("""SELECT utla_code, total_bed_days_lost, total_discharges_acceptable_trusts,
              discharged_no_delay, discharged_21_plus_days
              FROM nhs_drd_discharge_delays WHERE reporting_period='2026-05-01'""")
db = {r["utla_code"]: r for r in c2.fetchall()}
mm = 0
for code, rec in may.items():
    if code not in db:
        print("  in file not db:", code)
        mm += 1
        continue
    for col in ("total_bed_days_lost", "total_discharges_acceptable_trusts",
                "discharged_no_delay", "discharged_21_plus_days"):
        if rec.get(col) != db[code][col]:
            print("  MISMATCH %s %s: file=%s db=%s" % (code, col, rec.get(col), db[code][col]))
            mm += 1
print("VERIFY May 2026: %d UTLAs in file, %d in db, %d mismatches"
      % (len(may), len(db), mm))
if mm:
    sys.exit("May 2026 does not reconcile; not loading June")

# --- load June 2026 ----------------------------------------------------
jun = parse(os.path.join(HERE, "src/drd_June-2026.csv"), datetime.date(2026, 6, 1))
src = ("https://www.england.nhs.uk/statistics/statistical-work-areas/"
       "discharge-ready-date/ Discharge-Ready-Date-monthly-data-csv-June-2026.csv")
cols = ["reporting_period", "utla_code", "utla_name"] + list(MEASURES.values())
n = 0
for rec in jun.values():
    vals = [rec.get(c) for c in cols]
    sets = ", ".join("%s=EXCLUDED.%s" % (c, c) for c in cols[3:])
    cur.execute(
        "INSERT INTO nhs_drd_discharge_delays (%s, source, loaded_at) VALUES (%s, %%s, now()) "
        "ON CONFLICT (reporting_period, utla_code) DO UPDATE SET %s, source=EXCLUDED.source, loaded_at=now()"
        % (", ".join(cols), ", ".join(["%s"] * len(cols)), sets),
        vals + [src])
    n += cur.rowcount
conn.commit()
print("LOAD June 2026: %d rows written" % n)

c2.execute("""SELECT reporting_period, COUNT(*) utlas, SUM(total_bed_days_lost) bed_days
              FROM nhs_drd_discharge_delays WHERE reporting_period >= '2026-04-01'
              GROUP BY 1 ORDER BY 1""")
for r in c2.fetchall():
    print("   %s  %d UTLAs  %s bed days lost" % (r["reporting_period"], r["utlas"], r["bed_days"]))
c2.execute("""SELECT reporting_period, total_bed_days_lost, pct_delayed_1plus_days
              FROM nhs_drd_discharge_delays WHERE utla_code='E08000012'
              AND reporting_period >= '2026-05-01' ORDER BY 1""")
print("Liverpool:")
for r in c2.fetchall():
    print("   ", dict(r))
conn.close()
