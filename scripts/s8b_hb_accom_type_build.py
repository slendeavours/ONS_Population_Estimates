"""S8b - HB Accommodation Type Breakdown ETL.

Queries DWP Stat-Xplore for Housing Benefit caseload by accommodation type
(SA, TA, Other, Unknown) across English local authorities, loads into
la_hb_accom_type_caseload, and runs verification suite.
"""

import os
import sys
import json
import time
import re
import psycopg2
import psycopg2.extras
import requests
from pathlib import Path
from dotenv import load_dotenv

# Resolved relative to this file so no local path is baked into a public
# repository. Repository root first, then its parent, where the shared .env sits.
_HERE = Path(__file__).resolve().parent
load_dotenv(_HERE.parent / ".env")
load_dotenv(_HERE.parent.parent / ".env")

API_ROOT = "https://stat-xplore.dwp.gov.uk/webapi/rest/v1"
API_KEY = (
    os.environ.get("StatXplore_API_Key", "")
    or os.environ.get("STATXPLORE_API_KEY", "")
).strip()
if not API_KEY:
    sys.exit("HARD STOP: StatXplore_API_Key missing from environment.")

DB_HOST = (os.getenv("PG_HOST") or "localhost").replace("postgres", "localhost")

def _require_env(name):
    """Credentials must come from the environment. Never fall back to a
    literal: a default in source is a published credential."""
    value = os.environ.get(name)
    if not value:
        sys.exit(f"HARD STOP: {name} is not set. Set it in the environment "
                 f"or .env. This script will not guess a credential.")
    return value

DB_CFG = dict(
    host=DB_HOST,
    port=int(os.getenv("PG_PORT", "5432")),
    dbname=os.getenv("PG_DATABASE", "exempt_pipeline"),
    user=_require_env("PG_USER"),
    password=_require_env("PG_PASSWORD"),
)

HEADERS = {"APIKey": API_KEY, "Content-Type": "application/json"}

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

ACCOM_MEMBERS = [
    {"code": "SA", "label": "Specified Accommodation",
     "id": "str:value:hb_new:V_F_HB_NEW:SATA:C_SATA:1"},
    {"code": "TA", "label": "Temporary Accommodation",
     "id": "str:value:hb_new:V_F_HB_NEW:SATA:C_SATA:2"},
    {"code": "OTHER", "label": "Other",
     "id": "str:value:hb_new:V_F_HB_NEW:SATA:C_SATA:9"},
    {"code": "UNKNOWN", "label": "Unknown / Missing",
     "id": "str:value:hb_new:V_F_HB_NEW:SATA:C_SATA:99"},
]

MONTHS = [
    {"yyyymm": "202509", "label": "202509 (Sep-25)",
     "id": "str:value:hb_new:F_HB_NEW_DATE:NEW_DATE_NAME:C_HB_NEW_DATE:202509"},
    {"yyyymm": "202510", "label": "202510 (Oct-25)",
     "id": "str:value:hb_new:F_HB_NEW_DATE:NEW_DATE_NAME:C_HB_NEW_DATE:202510"},
    {"yyyymm": "202511", "label": "202511 (Nov-25)",
     "id": "str:value:hb_new:F_HB_NEW_DATE:NEW_DATE_NAME:C_HB_NEW_DATE:202511"},
    {"yyyymm": "202512", "label": "202512 (Dec-25)",
     "id": "str:value:hb_new:F_HB_NEW_DATE:NEW_DATE_NAME:C_HB_NEW_DATE:202512"},
    {"yyyymm": "202601", "label": "202601 (Jan-26)",
     "id": "str:value:hb_new:F_HB_NEW_DATE:NEW_DATE_NAME:C_HB_NEW_DATE:202601"},
    {"yyyymm": "202602", "label": "202602 (Feb-26)",
     "id": "str:value:hb_new:F_HB_NEW_DATE:NEW_DATE_NAME:C_HB_NEW_DATE:202602"},
]

DB_ID = "str:database:hb_new"
MEASURE_ID = "str:count:hb_new:V_F_HB_NEW"
GEO_FIELD_ID = "str:field:hb_new:V_F_HB_NEW:ADMIN_LA_CODE"
GEO_VALUESET_ID = "str:valueset:hb_new:V_F_HB_NEW:ADMIN_LA_CODE:V_C_ADMIN_LA"
DATE_FIELD_ID = "str:field:hb_new:F_HB_NEW_DATE:NEW_DATE_NAME"
SATA_FIELD_ID = "str:field:hb_new:V_F_HB_NEW:SATA"

_last_api_call = 0.0


def _throttle():
    global _last_api_call
    elapsed = time.time() - _last_api_call
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)
    _last_api_call = time.time()


def api_get(path, retries=3):
    url = path if path.startswith("http") else f"{API_ROOT}/{path.lstrip('/')}"
    _throttle()
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=120)
            if r.status_code == 503 and attempt < retries - 1:
                wait = 30 * (attempt + 1)
                print(f"  503 maintenance, retrying in {wait}s...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return json.loads(r.text), r.headers
        except requests.RequestException as e:
            if attempt < retries - 1:
                wait = 30 * (attempt + 1)
                print(f"  Error: {e}, retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise


def api_get_all_pages(path):
    all_children = []
    url = path if path.startswith("http") else f"{API_ROOT}/{path.lstrip('/')}"
    while url:
        data, headers = api_get(url)
        all_children.extend(data.get("children", []))
        link = headers.get("Link", "")
        m = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = m.group(1) if m else None
    return all_children


def api_post(path, body, retries=5):
    url = f"{API_ROOT}/{path.lstrip('/')}"
    _throttle()
    for attempt in range(retries):
        try:
            r = requests.post(url, headers=HEADERS, json=body, timeout=120)
            if r.status_code in (500, 502, 503, 504) and attempt < retries - 1:
                wait = 30 * (attempt + 1)
                print(f"  {r.status_code} on POST, retrying in {wait}s...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, requests.exceptions.ReadTimeout) as e:
            if attempt < retries - 1:
                wait = 30 * (attempt + 1)
                print(f"  Error on POST: {e}, retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise


def get_english_la_members():
    """Fetch English LA members from the admin geography valueset."""
    print("  Fetching geography members...")
    members = api_get_all_pages(f"/schema/{GEO_VALUESET_ID}")
    english = [m for m in members if m["id"].split(":")[-1].startswith("E")]
    print(f"  Total: {len(members)}, English: {len(english)}")
    return english


def resolve_geography(english_members, conn):
    """Map API geography codes to current LAD24CD, handling historical mergers."""
    code_to_uri = {}
    for m in english_members:
        uri = m["id"]
        code = uri.split(":")[-1]
        code_to_uri[code] = uri

    cur = conn.cursor()
    cur.execute("SELECT lad24cd FROM la_boundaries")
    current_lads = {r[0] for r in cur.fetchall()}

    cur.execute("SELECT old_code, new_code FROM la_code_lookup")
    code_lookup = {}
    for old, new in cur.fetchall():
        code_lookup.setdefault(old, []).append(new)

    lad_to_uris = {}
    unresolvable = []

    for code, uri in code_to_uri.items():
        if code in current_lads:
            lad_to_uris.setdefault(code, []).append(uri)
        elif code in code_lookup:
            targets = [t for t in code_lookup[code] if t in current_lads]
            if targets:
                for t in targets:
                    lad_to_uris.setdefault(t, []).append(uri)
            else:
                unresolvable.append(code)
        else:
            unresolvable.append(code)

    if unresolvable:
        print(f"  WARNING: Unresolvable codes: {unresolvable}")

    print(f"  Resolved to {len(lad_to_uris)} current LAD24CD codes")
    return lad_to_uris, list(code_to_uri.values())


def fetch_month_accom(month, accom_member, query_uris):
    """Query one month x one accom type, batched by geography."""
    BATCH_SIZE = 50
    batches = [query_uris[i:i + BATCH_SIZE]
               for i in range(0, len(query_uris), BATCH_SIZE)]
    all_results = {}

    for batch_num, batch_uris in enumerate(batches):
        q = {
            "database": DB_ID,
            "measures": [MEASURE_ID],
            "recodes": {
                GEO_FIELD_ID: {"map": [[u] for u in batch_uris], "total": False},
                DATE_FIELD_ID: {"map": [[month["id"]]], "total": False},
                SATA_FIELD_ID: {"map": [[accom_member["id"]]], "total": False},
            },
            "dimensions": [
                [GEO_FIELD_ID],
                [DATE_FIELD_ID],
                [SATA_FIELD_ID],
            ],
        }
        if batch_num > 0:
            time.sleep(2)

        resp = api_post("/table", q)
        cubes = resp["cubes"]
        cube_key = list(cubes.keys())[0]
        values = cubes[cube_key]["values"]
        items = resp["fields"][0]["items"]

        for i, item in enumerate(items):
            code = item["uris"][0].split(":")[-1]
            v = values[i]
            while isinstance(v, list):
                v = v[0]
            all_results[code] = v

    return all_results


def phase2_etl():
    print("=" * 60)
    print("PHASE 2: ETL to Postgres")
    print("=" * 60)

    conn = psycopg2.connect(**DB_CFG)
    conn.autocommit = False

    try:
        cur = conn.cursor()

        # Create table
        print("\n  Creating table...")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS la_hb_accom_type_caseload (
                lad24cd      TEXT NOT NULL,
                month        TEXT NOT NULL,
                accom_type   TEXT NOT NULL,
                claimants    INTEGER,
                loaded_at    TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (lad24cd, month, accom_type)
            );
        """)
        conn.commit()
        print("  Table ready.")

        # Get geography members and resolve
        english_members = get_english_la_members()
        lad_to_uris, all_query_uris = resolve_geography(english_members, conn)

        # Fetch and upsert for each month x accom type
        total_upserted = 0
        months_loaded = []

        for month in MONTHS:
            print(f"\n  Month: {month['label']}")
            for accom in ACCOM_MEMBERS:
                print(f"    Accom: {accom['code']} ({accom['label']})...")
                raw = fetch_month_accom(month, accom, all_query_uris)

                rows = []
                for lad, uris in lad_to_uris.items():
                    source_codes = [u.split(":")[-1] for u in uris]
                    vals = [raw.get(c) for c in source_codes]
                    non_null = [v for v in vals if v is not None]
                    total = sum(non_null) if non_null else None
                    rows.append({
                        "lad24cd": lad,
                        "month": month["yyyymm"],
                        "accom_type": accom["code"],
                        "claimants": total,
                    })

                batch_json = json.dumps(rows)
                cur.execute("""
                    INSERT INTO la_hb_accom_type_caseload
                        (lad24cd, month, accom_type, claimants)
                    SELECT r.lad24cd, r.month, r.accom_type, r.claimants
                    FROM json_to_recordset(%s::json)
                        AS r(lad24cd text, month text, accom_type text, claimants int)
                    ON CONFLICT (lad24cd, month, accom_type) DO UPDATE SET
                        claimants = EXCLUDED.claimants,
                        loaded_at = NOW();
                """, (batch_json,))
                upserted = cur.rowcount
                total_upserted += upserted
                conn.commit()
                print(f"      Upserted {upserted} rows")

            months_loaded.append(month["yyyymm"])

        # Log to pipeline_run_log
        log_notes = (
            f"Months: {', '.join(months_loaded)}. "
            f"Categories: SA, TA, OTHER, UNKNOWN. "
            f"Total rows: {total_upserted}."
        )
        cur.execute("""
            INSERT INTO pipeline_run_log
                (agent_name, source_number, rows_written, status, notes)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id;
        """, ("Source 8b - HB Accommodation Type", "8", total_upserted,
              "success", log_notes))
        log_id = cur.fetchone()[0]
        conn.commit()
        print(f"\n  pipeline_run_log id={log_id}, total upserted={total_upserted}")

        return conn, total_upserted, log_id, lad_to_uris

    except Exception:
        conn.close()
        raise


def phase3_verify(conn):
    print("\n" + "=" * 60)
    print("PHASE 3: Verification Suite")
    print("=" * 60)

    cur = conn.cursor()
    all_pass = True

    # 1. Coverage gate: every month has 296 distinct lad24cd for SA
    print("\n  1. Coverage gate (SA, 296 per month)...")
    cur.execute("""
        SELECT month, COUNT(DISTINCT lad24cd) AS cnt
        FROM la_hb_accom_type_caseload
        WHERE accom_type = 'SA'
        GROUP BY month ORDER BY month
    """)
    coverage = cur.fetchall()
    for month, cnt in coverage:
        status = "PASS" if cnt == 296 else "FAIL"
        if cnt != 296:
            all_pass = False
        print(f"    {month}: {cnt}/296 [{status}]")

    # 2. Boundary check: no lad24cd absent from la_boundaries
    print("\n  2. Boundary check...")
    cur.execute("""
        SELECT DISTINCT a.lad24cd
        FROM la_hb_accom_type_caseload a
        LEFT JOIN la_boundaries b ON b.lad24cd = a.lad24cd
        WHERE b.lad24cd IS NULL
    """)
    orphans = [r[0] for r in cur.fetchall()]
    if orphans:
        print(f"    FAIL: Orphan codes: {orphans}")
        all_pass = False
    else:
        print("    PASS: All codes present in la_boundaries")

    # 3. Anchor set: Birmingham, Manchester, Liverpool non-zero SA in latest month
    print("\n  3. Anchor set (latest month SA)...")
    cur.execute("""
        SELECT lad24cd, claimants
        FROM la_hb_accom_type_caseload
        WHERE accom_type = 'SA'
          AND month = (SELECT MAX(month) FROM la_hb_accom_type_caseload)
          AND lad24cd IN ('E08000025', 'E08000003', 'E08000012')
        ORDER BY lad24cd
    """)
    anchors = cur.fetchall()
    anchor_names = {"E08000025": "Birmingham", "E08000003": "Manchester",
                    "E08000012": "Liverpool"}
    for code, val in anchors:
        status = "PASS" if val and val > 0 else "FAIL"
        if not val or val <= 0:
            all_pass = False
        print(f"    {anchor_names.get(code, code)}: {val} [{status}]")

    # 4. Consistency check: compare SA vs la_hb_sa_caseload for Nov-25
    print("\n  4. Consistency check (SA vs la_hb_sa_caseload, Nov-25)...")
    cur.execute("""
        SELECT a.lad24cd,
               a.claimants AS new_sa,
               h.hb_sa_claimants AS old_sa
        FROM la_hb_accom_type_caseload a
        JOIN la_hb_sa_caseload h ON h.lad24cd = a.lad24cd AND h.month = '202511'
        WHERE a.month = '202511' AND a.accom_type = 'SA'
          AND a.lad24cd IN ('E08000025', 'E08000003', 'E08000012')
        ORDER BY a.lad24cd
    """)
    consistency = cur.fetchall()
    for code, new_sa, old_sa in consistency:
        if old_sa and old_sa > 0:
            pct_diff = abs(new_sa - old_sa) / old_sa * 100
            status = "PASS" if pct_diff <= 10 else "HALT"
            if pct_diff > 10:
                all_pass = False
                print(f"    {anchor_names.get(code, code)}: new={new_sa}, old={old_sa}, "
                      f"diff={pct_diff:.1f}% [{status}] - REVIEW NEEDED")
            else:
                print(f"    {anchor_names.get(code, code)}: new={new_sa}, old={old_sa}, "
                      f"diff={pct_diff:.1f}% [{status}]")
        else:
            print(f"    {anchor_names.get(code, code)}: new={new_sa}, old={old_sa} [SKIP - no base]")

    # 5. Reasonableness: national totals
    print("\n  5. Reasonableness (national totals)...")
    cur.execute("""
        SELECT month, accom_type, SUM(claimants) AS total
        FROM la_hb_accom_type_caseload
        GROUP BY month, accom_type
        ORDER BY month, accom_type
    """)
    totals = cur.fetchall()
    summary_rows = []
    for month, atype, total in totals:
        total = total or 0
        flag = ""
        if atype == "SA":
            if total < 100000 or total > 700000:
                flag = " HALT"
                all_pass = False
        elif atype == "TA":
            if total < 50000 or total > 250000:
                flag = " HALT"
                all_pass = False
        elif atype in ("OTHER", "UNKNOWN"):
            if total < 0:
                flag = " FAIL"
                all_pass = False
        print(f"    {month} {atype:10s} {total:>10,}{flag}")
        summary_rows.append((month, atype, total))

    # Summary table
    print("\n  Summary: month x accom_type x national total")
    print(f"  {'Month':<8} {'SA':>10} {'TA':>10} {'OTHER':>10} {'UNKNOWN':>10}")
    by_month = {}
    for month, atype, total in summary_rows:
        by_month.setdefault(month, {})[atype] = total or 0
    for month in sorted(by_month.keys()):
        d = by_month[month]
        print(f"  {month:<8} {d.get('SA', 0):>10,} {d.get('TA', 0):>10,} "
              f"{d.get('OTHER', 0):>10,} {d.get('UNKNOWN', 0):>10,}")

    if not all_pass:
        print("\n  VERIFICATION FAILED - halting.")
        sys.exit(1)

    print("\n  All verification checks PASSED")
    return summary_rows


def main():
    conn, total_upserted, log_id, lad_to_uris = phase2_etl()
    try:
        summary = phase3_verify(conn)
        print(f"\n{'=' * 60}")
        print("PHASES 2-3 COMPLETE")
        print(f"{'=' * 60}")
        print(f"  Total rows upserted: {total_upserted}")
        print(f"  Pipeline log id: {log_id}")
        print(f"  Verification: ALL PASSED")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
