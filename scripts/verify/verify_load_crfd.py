"""Verify the loaded MHS26 month against publication, then load June 2026."""
import os
import io
import re
import csv
import sys
import zipfile
import datetime
import urllib.request
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv(r"C:\Users\slewi\ucws-repo\.env")
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "src")
os.makedirs(SRC, exist_ok=True)
BASE = ("https://digital.nhs.uk/data-and-information/publications/statistical/"
        "mental-health-services-monthly-statistics/")
# 'Rstr' is the Restrictive Interventions file and shares the Prf suffix.
EXCLUDE = ("final", "restrictive", "rstr", "oaps", "ascof", "4ww")


def discover(slug):
    r = urllib.request.Request(BASE + slug, headers={"User-Agent": "Mozilla/5.0"})
    h = urllib.request.urlopen(r, timeout=120).read().decode("utf8", "ignore")
    links = re.findall(r'href="(https://files\.digital\.nhs\.uk[^"]+)"', h)
    cand = [l for l in links if re.search(r"MHSDS%20Data_\w*(Prf|Perf)", l, re.I)]
    cand = [l for l in cand
            if not any(x in l.lower() for x in EXCLUDE)]
    return cand[0] if cand else None


def fetch(url, name):
    p = os.path.join(SRC, name)
    if not os.path.exists(p):
        r = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(r, timeout=600) as resp, open(p, "wb") as fh:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
    return p


def parse_mhs26(path):
    """Return {lad24cd: (la_name, value_or_None)} for the MHS26 LA breakdown."""
    out = {}
    zf = zipfile.ZipFile(path)
    names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
    for n in names:
        with zf.open(n) as fh:
            rdr = csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig"))
            if not rdr.fieldnames or "MEASURE_ID" not in rdr.fieldnames:
                continue
            for row in rdr:
                if row.get("MEASURE_ID") != "MHS26":
                    continue
                if "local authority" not in str(row.get("BREAKDOWN", "")).lower():
                    continue
                code = str(row.get("PRIMARY_LEVEL", "")).strip()
                if not code[:3] in ("E06", "E07", "E08", "E09"):
                    continue
                if str(row.get("SECONDARY_LEVEL", "")).strip().upper() != "NONE":
                    continue
                raw = str(row.get("MEASURE_VALUE", "")).strip()
                val = None
                if raw not in ("*", "", "NULL"):
                    try:
                        val = int(float(raw))
                    except ValueError:
                        val = None
                out[code] = (str(row.get("PRIMARY_LEVEL_DESCRIPTION", "")).strip(), val)
    return out


conn = psycopg2.connect(host="localhost", port=os.getenv("PG_PORT"),
                        dbname=os.getenv("PG_DATABASE"), user=os.getenv("PG_USER"),
                        password=os.getenv("PG_PASSWORD"))
cur = conn.cursor()
c2 = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

# MHSDS publishes Barnsley and Sheffield on their post-April-2025 codes while
# la_boundaries retains the originals; la_code_lookup carries the mapping.
c2.execute("SELECT old_code, new_code FROM la_code_lookup")
RECODE = {r["old_code"]: r["new_code"] for r in c2.fetchall()}


def canon(code):
    return RECODE.get(code, code)


# --- verify May 2026 ----------------------------------------------------
u = discover("performance-may-2026")
print("May 2026 file: %s" % (u.split("/")[-1] if u else "NOT FOUND"))
may = parse_mhs26(fetch(u, "mhsds_May2026.zip"))
c2.execute("""SELECT lad24cd, measure_value FROM nhs_mh_crfd
              WHERE reporting_period='2026-05-01'""")
db = {r["lad24cd"]: r["measure_value"] for r in c2.fetchall()}
may = {canon(k): v for k, v in may.items()}
mm = [(k, may[k][1], db.get(k)) for k in may if db.get(k) != may[k][1]]
print("VERIFY May 2026: %d LAs in file, %d in db, %d mismatches"
      % (len(may), len(db), len(mm)))
for m in mm[:6]:
    print("   ", m)
if mm:
    sys.exit("May does not reconcile; not loading June")

# --- load June 2026 -----------------------------------------------------
u6 = discover("performance-june-2026")
print("June 2026 file: %s" % (u6.split("/")[-1] if u6 else "NOT FOUND"))
jun = {canon(k): v for k, v in parse_mhs26(fetch(u6, "mhsds_Jun2026.zip")).items()}
MEASURE_NAME = ("Days of delayed discharge, for patients clinically ready for "
                "discharge, in the Reporting Period")
n = 0
for code, (name, val) in jun.items():
    cur.execute("""
        INSERT INTO nhs_mh_crfd (reporting_period, lad24cd, la_name, measure_id,
            measure_name, measure_value, source, loaded_at)
        VALUES (%s,%s,%s,'MHS26',%s,%s,%s,now())
        ON CONFLICT (reporting_period, lad24cd, measure_id) DO UPDATE SET
            la_name=EXCLUDED.la_name, measure_value=EXCLUDED.measure_value,
            source=EXCLUDED.source, loaded_at=now()
    """, (datetime.date(2026, 6, 1), code, name, MEASURE_NAME, val, u6))
    n += cur.rowcount
conn.commit()
print("LOAD June 2026: %d rows written" % n)

c2.execute("""SELECT reporting_period, COUNT(*) las,
              COUNT(measure_value) reported, SUM(measure_value) days
              FROM nhs_mh_crfd WHERE reporting_period >= '2026-04-01'
              GROUP BY 1 ORDER BY 1""")
for r in c2.fetchall():
    supp = r["las"] - r["reported"]
    print("   %s  %d LAs, %d reported, %d suppressed (%.0f%%), %s days"
          % (r["reporting_period"], r["las"], r["reported"], supp,
             supp * 100.0 / r["las"], r["days"]))
c2.execute("""SELECT reporting_period, measure_value FROM nhs_mh_crfd
              WHERE lad24cd='E08000012' AND reporting_period >= '2026-05-01'
              ORDER BY 1""")
print("Liverpool:", [dict(r) for r in c2.fetchall()])
conn.close()
