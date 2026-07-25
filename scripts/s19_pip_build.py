"""S19 — PIP Claimants ETL: Stat-Xplore schema discovery, fetch, and Postgres load.

Queries the DWP Stat-Xplore REST API for Personal Independence Payment
cases with entitlement by English local authority, then loads into
la_pip_claimants in the exempt_pipeline database.
"""

import os
import sys
import json
import time
import re
import datetime
import psycopg2
import psycopg2.extras
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Config ──────────────────────────────────────────────────────────────────

API_ROOT = "https://stat-xplore.dwp.gov.uk/webapi/rest/v1"
API_KEY = os.environ.get("Stat-Xplore_Token", "")
if not API_KEY:
    sys.exit("HARD STOP: Stat-Xplore_Token missing from environment.")

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

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
CACHE_DIR = REPO_ROOT / "s19_cache"
CACHE_DIR.mkdir(exist_ok=True)

HEADERS = {"APIKey": API_KEY, "Content-Type": "application/json"}

_last_api_call = 0.0


def _throttle():
    global _last_api_call
    elapsed = time.time() - _last_api_call
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)
    _last_api_call = time.time()


def api_get(url_or_path, retries=3, timeout=180):
    if url_or_path.startswith("http"):
        url = url_or_path
    else:
        url = f"{API_ROOT}/{url_or_path.lstrip('/')}"
    _throttle()
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            if r.status_code == 503 and attempt < retries - 1:
                wait = 5 * (5 ** attempt)
                print(f"  503 on GET, retrying in {wait}s...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return json.loads(r.text), r.headers
        except requests.RequestException as e:
            if attempt < retries - 1:
                wait = 5 * (5 ** attempt)
                print(f"  Error: {e}, retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise
    return None, {}


def api_get_all_pages(url_or_path):
    """GET with automatic pagination — follows Link rel=next headers."""
    all_children = []
    if url_or_path.startswith("http"):
        url = url_or_path
    else:
        url = f"{API_ROOT}/{url_or_path.lstrip('/')}"
    page = 0
    while url:
        data, headers = api_get(url)
        children = data.get("children", [])
        all_children.extend(children)
        link = headers.get("Link", "")
        next_match = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = next_match.group(1) if next_match else None
        page += 1
    return all_children


def api_post(path, body, retries=5):
    url = f"{API_ROOT}/{path.lstrip('/')}"
    _throttle()
    for attempt in range(retries):
        try:
            r = requests.post(url, headers=HEADERS, json=body, timeout=120)
            if r.status_code in (500, 502, 503, 504) and attempt < retries - 1:
                wait = 10 * (2 ** attempt)
                print(f"  {r.status_code} on POST, retrying in {wait}s...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, requests.exceptions.ReadTimeout) as e:
            if attempt < retries - 1:
                wait = 10 * (2 ** attempt)
                print(f"  Error on POST: {e}, retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise
    return None


# ── Phase 1: Schema Discovery ──────────────────────────────────────────────

def discover_schema():
    print("=== Phase 1: Schema Discovery ===")

    root, _ = api_get("/schema")
    pip_databases = []
    all_databases = []

    def walk_folders(node, depth=0):
        node_type = node.get("type", "")
        if node_type == "DATABASE":
            all_databases.append(node)
            combined = (node.get("label", "") + " " + node.get("id", "")).lower()
            if "pip" in combined or "personal independence" in combined:
                pip_databases.append(node)
            return
        for child_ref in node.get("children", []):
            child_id = child_ref if isinstance(child_ref, str) else child_ref.get("id", "")
            if child_id:
                child, _ = api_get(f"/schema/{child_id}")
                walk_folders(child, depth + 1)

    walk_folders(root)

    if not pip_databases:
        sys.exit("HARD STOP: No PIP database found in Stat-Xplore schema.")

    print(f"  Found {len(pip_databases)} PIP database(s):")
    alternatives = []
    selected = None
    for db in pip_databases:
        label = db.get("label", "")
        db_id = db.get("id", "")
        print(f"    - {label} ({db_id})")
        alternatives.append({"label": label, "id": db_id})
        lower = label.lower()
        if "cases with entitlement" in lower and "from" in lower:
            selected = db

    if not selected:
        for db in pip_databases:
            if "cases with entitlement" in db.get("label", "").lower():
                selected = db
                break
    if not selected:
        selected = pip_databases[0]

    print(f"  Selected: {selected['label']} ({selected['id']})")

    # Get database children
    db_schema, _ = api_get(f"/schema/{selected['id']}")
    children = db_schema.get("children", [])

    measure_id = None
    geo_group = None
    dl_field_ref = None
    date_field_ref = None
    all_fields_refs = []

    for child in children:
        ctype = child.get("type", "")
        clabel = child.get("label", "").lower()
        cid = child.get("id", "")

        if ctype == "COUNT":
            measure_id = cid
            measure_label = child.get("label", "")
        elif ctype == "GROUP" and "geography" in clabel:
            geo_group = child
        elif ctype == "FIELD":
            all_fields_refs.append(child)
            if "daily living" in clabel:
                dl_field_ref = child
            elif clabel in ("month", "quarter", "date", "period", "time"):
                date_field_ref = child

    if not measure_id:
        sys.exit("HARD STOP: No caseload measure (COUNT) found.")
    if not geo_group:
        sys.exit("HARD STOP: No geography group found.")
    if not dl_field_ref:
        sys.exit("HARD STOP: No daily living award field found.")
    if not date_field_ref:
        sys.exit("HARD STOP: No date field found.")

    print(f"  Measure: {measure_label} ({measure_id})")

    # Walk geography group to find LA field
    geo_detail, _ = api_get(f"/schema/{geo_group['id']}")
    geo_fields = geo_detail.get("children", [])
    la_field = None
    for gf in geo_fields:
        gf_label = gf.get("label", "").lower()
        if "la" in gf_label or "local authority" in gf_label:
            la_field = gf
            break
    if not la_field:
        la_field = geo_fields[0]

    print(f"  Geography field: {la_field['label']} ({la_field['id']})")

    # Get geography field valuesets — record labels but only fetch
    # members for the LA-level valueset (OA/LSOA/MSOA are too large).
    geo_field_detail, _ = api_get(f"/schema/{la_field['id']}")
    geo_valuesets = geo_field_detail.get("children", [])
    geo_valuesets_info = []
    la_valueset_id = None
    la_valueset_members = None

    for vs in geo_valuesets:
        vs_label = vs.get("label", "")
        vs_id = vs.get("id", "")
        geo_valuesets_info.append({"label": vs_label, "id": vs_id})
        print(f"    Valueset: {vs_label}")
        if "local authority" in vs_label.lower():
            la_valueset_id = vs_id
            la_valueset_members = api_get_all_pages(f"/schema/{vs_id}")
            geo_valuesets_info[-1]["member_count"] = len(la_valueset_members)
            print(f"      -> {len(la_valueset_members)} members (fetched)")

    if la_valueset_id is None:
        sys.exit("HARD STOP: No LA-level geography valueset found.")

    english_members = [
        m for m in la_valueset_members if m["id"].split(":")[-1].startswith("E")
    ]
    print(f"  LA valueset: {la_valueset_id} — {len(la_valueset_members)} total, {len(english_members)} English")

    # Daily living — find Enhanced
    dl_detail, _ = api_get(f"/schema/{dl_field_ref['id']}")
    dl_vs = dl_detail.get("children", [])
    enhanced_id = None
    dl_valueset_id = None
    for vs in dl_vs:
        if vs.get("type") == "VALUESET":
            dl_valueset_id = vs["id"]
            dl_members = api_get_all_pages(f"/schema/{vs['id']}")
            for m in dl_members:
                if "enhanced" in m.get("label", "").lower():
                    enhanced_id = m["id"]
                    print(f"  Enhanced DL: {m['label']} ({enhanced_id})")
                    break
            break

    if not enhanced_id:
        sys.exit("HARD STOP: No 'Enhanced' member found in daily living field.")

    # Date — latest month
    date_detail, _ = api_get(f"/schema/{date_field_ref['id']}")
    date_vs = date_detail.get("children", [])
    date_valueset_id = None
    latest_month_id = None
    latest_month_label = None
    date_member_count = 0
    for vs in date_vs:
        if vs.get("type") == "VALUESET":
            date_valueset_id = vs["id"]
            date_members = api_get_all_pages(f"/schema/{vs['id']}")
            date_member_count = len(date_members)
            if date_members:
                latest = date_members[-1]
                latest_month_id = latest["id"]
                latest_month_label = latest.get("label", "")
                print(f"  Latest month: {latest_month_label} ({latest_month_id})")
                print(f"  Date valueset: {date_member_count} periods")
            break

    if not latest_month_id:
        sys.exit("HARD STOP: No date members found.")

    discovery = {
        "database": {"label": selected["label"], "id": selected["id"]},
        "alternatives_considered": alternatives,
        "measure": {"label": measure_label, "id": measure_id},
        "geography_field_id": la_field["id"],
        "geography_field_label": la_field["label"],
        "geography_valuesets": geo_valuesets_info,
        "la_valueset_id": la_valueset_id,
        "la_english_members": english_members,
        "daily_living_field_id": dl_field_ref["id"],
        "daily_living_field_label": dl_field_ref.get("label", ""),
        "dl_valueset_id": dl_valueset_id,
        "enhanced_member_id": enhanced_id,
        "date_field_id": date_field_ref["id"],
        "date_field_label": date_field_ref.get("label", ""),
        "date_valueset_id": date_valueset_id,
        "date_member_count": date_member_count,
        "latest_month": {"label": latest_month_label, "id": latest_month_id},
    }

    # Cache discovery
    (CACHE_DIR / "discovery.json").write_text(
        json.dumps(discovery, indent=2), encoding="utf-8"
    )
    return discovery


# ── Phase 2: Geography Resolution ──────────────────────────────────────────

def resolve_geography(discovery, conn):
    print("\n=== Phase 2: Geography Resolution ===")
    english_members = discovery["la_english_members"]

    code_to_uri = {}
    for m in english_members:
        uri = m["id"]
        code = uri.split(":")[-1]
        code_to_uri[code] = uri
    print(f"  Extracted {len(code_to_uri)} English codes")

    cur = conn.cursor()
    cur.execute("SELECT lad24cd FROM la_boundaries")
    current_lads = {r[0] for r in cur.fetchall()}

    cur.execute("SELECT old_code, new_code FROM la_code_lookup")
    code_lookup = {}
    for old, new in cur.fetchall():
        code_lookup.setdefault(old, []).append(new)

    direct = {}
    historical = {}
    unresolvable = []
    historical_sum_map = {}

    for code, uri in code_to_uri.items():
        if code in current_lads:
            direct[code] = uri
        elif code in code_lookup:
            targets = [t for t in code_lookup[code] if t in current_lads]
            if targets:
                for t in targets:
                    historical[code] = {"uri": uri, "target": t}
                    historical_sum_map.setdefault(t, []).append(code)
            else:
                unresolvable.append(code)
        else:
            unresolvable.append(code)

    if unresolvable:
        sys.exit(f"HARD STOP: Unresolvable codes: {unresolvable}")

    resolved_lads = set(direct.keys()) | {v["target"] for v in historical.values()}
    coverage_count = len(resolved_lads)
    coverage_pct = round(coverage_count / 296 * 100, 1)

    if coverage_pct >= 95:
        confidence = "High"
    elif coverage_pct >= 50:
        confidence = "Medium"
    else:
        confidence = "Low"

    sum_needed = {k: v for k, v in historical_sum_map.items() if len(v) > 1}

    print(f"  Direct: {len(direct)}, Historical: {len(historical)}")
    print(f"  Coverage: {coverage_count}/296 ({coverage_pct}%)")
    print(f"  Confidence: {confidence}")
    if sum_needed:
        print(f"  Summing needed for {len(sum_needed)} LAD(s)")

    # Map resolved LAD24CD -> list of source URIs
    lad_to_uris = {}
    for code, uri in direct.items():
        lad_to_uris.setdefault(code, []).append(uri)
    for code, info in historical.items():
        lad_to_uris.setdefault(info["target"], []).append(info["uri"])

    return {
        "direct_count": len(direct),
        "historical_count": len(historical),
        "coverage_count": coverage_count,
        "coverage_pct": coverage_pct,
        "confidence": confidence,
        "sum_needed": dict(sum_needed),
        "lad_to_uris": lad_to_uris,
        "all_query_uris": list(code_to_uri.values()),
    }


# ── Phase 3: Query Build and Fetch ─────────────────────────────────────────

def build_and_fetch(discovery, geo):
    print("\n=== Phase 3: Query Build and Fetch ===")
    db_id = discovery["database"]["id"]
    measure_id = discovery["measure"]["id"]
    geo_field_id = discovery["geography_field_id"]
    date_field_id = discovery["date_field_id"]
    dl_field_id = discovery["daily_living_field_id"]
    latest_month_id = discovery["latest_month"]["id"]
    enhanced_id = discovery["enhanced_member_id"]

    query_uris = geo["all_query_uris"]
    BATCH_SIZE = 15

    def parse_batch_response(resp):
        cubes = resp["cubes"]
        cube_key = list(cubes.keys())[0]
        values = cubes[cube_key]["values"]
        items = resp["fields"][0]["items"]
        results = {}
        for i, item in enumerate(items):
            code = item["uris"][0].split(":")[-1]
            v = values[i]
            while isinstance(v, list):
                v = v[0]
            results[code] = v
        return results

    def batched_fetch(query_type, extra_recodes=None, extra_dims=None):
        all_results = {}
        batches = [
            query_uris[i:i + BATCH_SIZE]
            for i in range(0, len(query_uris), BATCH_SIZE)
        ]
        annotations_out = {}
        for batch_num, batch_uris in enumerate(batches):
            recodes = {
                geo_field_id: {"map": [[u] for u in batch_uris], "total": False},
                date_field_id: {"map": [[latest_month_id]], "total": False},
            }
            dims = [[geo_field_id], [date_field_id]]
            if extra_recodes:
                recodes.update(extra_recodes)
            if extra_dims:
                dims.extend(extra_dims)

            q = {
                "database": db_id,
                "measures": [measure_id],
                "recodes": recodes,
                "dimensions": dims,
            }
            if batch_num > 0:
                time.sleep(3)
            print(f"    {query_type} batch {batch_num + 1}/{len(batches)} ({len(batch_uris)} LAs)...")
            resp = api_post("/table", q)
            batch_results = parse_batch_response(resp)
            all_results.update(batch_results)

            if "annotationMap" in resp and not annotations_out:
                annotations_out = resp["annotationMap"]

        return all_results, annotations_out

    # Save representative query bodies (first batch, no secrets)
    first_batch = query_uris[:BATCH_SIZE]
    q1_sample = {
        "database": db_id,
        "measures": [measure_id],
        "recodes": {
            geo_field_id: {"map": [[u] for u in first_batch], "total": False},
            date_field_id: {"map": [[latest_month_id]], "total": False},
        },
        "dimensions": [[geo_field_id], [date_field_id]],
    }
    q2_sample = {
        **q1_sample,
        "recodes": {
            **q1_sample["recodes"],
            dl_field_id: {"map": [[enhanced_id]], "total": False},
        },
        "dimensions": [[geo_field_id], [date_field_id], [dl_field_id]],
    }
    (REPO_ROOT / "s19_query_total.json").write_text(
        json.dumps(q1_sample, indent=2), encoding="utf-8"
    )
    (REPO_ROOT / "s19_query_enhanced_dl.json").write_text(
        json.dumps(q2_sample, indent=2), encoding="utf-8"
    )
    print(f"  Saved query JSONs (sample of first {BATCH_SIZE} LAs)")

    print("  Fetching total caseload...")
    total_raw, total_ann = batched_fetch("total")
    print("  Fetching enhanced daily living...")
    enhanced_raw, enhanced_ann = batched_fetch(
        "enhanced_dl",
        extra_recodes={dl_field_id: {"map": [[enhanced_id]], "total": False}},
        extra_dims=[[dl_field_id]],
    )
    print(f"  Parsed {len(total_raw)} total, {len(enhanced_raw)} enhanced DL values")

    # Capture annotations
    annotations = {}
    if total_ann:
        annotations["total"] = total_ann
    if enhanced_ann:
        annotations["enhanced_dl"] = enhanced_ann

    # Resolve to LAD24CD, applying historical-code summing
    lad_to_uris = geo["lad_to_uris"]
    month_label = discovery["latest_month"]["label"]
    rows = []
    for lad, uris in lad_to_uris.items():
        source_codes = [u.split(":")[-1] for u in uris]
        total_vals = [total_raw.get(c) for c in source_codes]
        enhanced_vals = [enhanced_raw.get(c) for c in source_codes]

        non_null_total = [v for v in total_vals if v is not None]
        non_null_enhanced = [v for v in enhanced_vals if v is not None]

        total = sum(non_null_total) if non_null_total else None
        enhanced = sum(non_null_enhanced) if non_null_enhanced else None

        rows.append({
            "lad24cd": lad,
            "month": month_label,
            "pip_total_claimants": total,
            "pip_enhanced_daily_living": enhanced,
        })

    print(f"  Resolved {len(rows)} LAD rows")
    return rows, annotations


# ── Phase 4: Table Create and Load ─────────────────────────────────────────

def create_and_load(conn, rows, discovery, geo, annotations):
    print("\n=== Phase 4: Table Create and Load ===")
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS la_pip_claimants (
            lad24cd text NOT NULL,
            month text NOT NULL,
            pip_total_claimants integer,
            pip_enhanced_daily_living integer,
            loaded_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (lad24cd, month)
        );
    """)
    cur.execute("ALTER TABLE la_pip_claimants OWNER TO pipeline_user")
    conn.commit()

    month_label = discovery["latest_month"]["label"]
    db_id = discovery["database"]["id"]
    cov = geo["coverage_count"]
    cov_pct = geo["coverage_pct"]
    confidence = geo["confidence"]

    # Build annotation text from annotationMap
    annotation_text = ""
    ann_map = annotations.get("total", {})
    if ann_map:
        for key in sorted(ann_map.keys()):
            if key == "I":
                annotation_text += f' DWP note: {ann_map[key]}'

    table_comment = (
        f"PIP cases with entitlement by English local authority. "
        f"Source: DWP Stat-Xplore, database {db_id}. "
        f"Month loaded: {month_label}. "
        f"Coverage: {cov}/296 ({cov_pct}%). Confidence: {confidence}. "
        f"Absence of a row means no published data for that LA, not zero."
    )
    cur.execute("COMMENT ON TABLE la_pip_claimants IS %s", (table_comment,))

    sum_note = ""
    if geo["sum_needed"]:
        summed_lads = list(geo["sum_needed"].keys())
        sum_note = f" Historical-code summing applied for: {', '.join(summed_lads)}."

    col_total_comment = (
        f"Total PIP cases with entitlement.{annotation_text}{sum_note}"
    )
    col_enhanced_comment = (
        f"PIP cases with Enhanced daily living award — sharper HSS demand signal "
        f"than total caseload (disability is the core eligibility criterion for "
        f"supported living placement demand).{annotation_text}{sum_note}"
    )
    cur.execute(
        "COMMENT ON COLUMN la_pip_claimants.pip_total_claimants IS %s",
        (col_total_comment,)
    )
    cur.execute(
        "COMMENT ON COLUMN la_pip_claimants.pip_enhanced_daily_living IS %s",
        (col_enhanced_comment,)
    )
    conn.commit()

    # Upsert
    batch_json = json.dumps(rows)
    cur.execute("""
        INSERT INTO la_pip_claimants (lad24cd, month, pip_total_claimants, pip_enhanced_daily_living)
        SELECT r.lad24cd, r.month, r.pip_total_claimants, r.pip_enhanced_daily_living
        FROM json_to_recordset(%s::json)
            AS r(lad24cd text, month text, pip_total_claimants int, pip_enhanced_daily_living int)
        ON CONFLICT (lad24cd, month) DO UPDATE SET
            pip_total_claimants = EXCLUDED.pip_total_claimants,
            pip_enhanced_daily_living = EXCLUDED.pip_enhanced_daily_living,
            loaded_at = NOW();
    """, (batch_json,))
    rows_written = cur.rowcount
    conn.commit()
    print(f"  Upserted {rows_written} rows")

    # Log to pipeline_run_log
    log_notes = (
        f"Month: {month_label}. Coverage: {cov}/296 ({cov_pct}%). "
        f"Confidence: {confidence}."
    )
    cur.execute("""
        INSERT INTO pipeline_run_log (agent_name, source_number, rows_written, status, notes)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id;
    """, ("Source 19 - PIP Claimants", 19, rows_written, "success", log_notes))
    log_id = cur.fetchone()[0]
    conn.commit()
    print(f"  pipeline_run_log entry: id={log_id}")

    return rows_written, log_id


# ── Phase 5: Verification Suite ────────────────────────────────────────────

def verify(conn, geo, discovery, rows_written, log_id):
    print("\n=== Phase 5: Verification Suite ===")
    cur = conn.cursor()
    month_label = discovery["latest_month"]["label"]
    expected_count = geo["coverage_count"]
    results = {}

    # 1. Row count
    cur.execute(
        "SELECT count(*) FROM la_pip_claimants WHERE month = %s", (month_label,)
    )
    actual = cur.fetchone()[0]
    check1 = actual == expected_count
    results["row_count"] = {"pass": check1, "expected": expected_count, "actual": actual}
    print(f"  1. Row count: {'PASS' if check1 else 'FAIL'} ({actual}/{expected_count})")

    # 2. Integrity
    cur.execute("""
        SELECT count(*) FROM la_pip_claimants
        WHERE month = %s AND lad24cd IS NULL
    """, (month_label,))
    null_count = cur.fetchone()[0]
    cur.execute("""
        SELECT count(*) FROM la_pip_claimants p
        WHERE p.month = %s
        AND NOT EXISTS (SELECT 1 FROM la_boundaries b WHERE b.lad24cd = p.lad24cd)
    """, (month_label,))
    orphan_count = cur.fetchone()[0]
    check2 = null_count == 0 and orphan_count == 0
    results["integrity"] = {"pass": check2, "null": null_count, "orphan": orphan_count}
    print(f"  2. Integrity: {'PASS' if check2 else 'FAIL'} (null={null_count}, orphan={orphan_count})")

    # 3. Consistency
    cur.execute("""
        SELECT count(*) FROM la_pip_claimants
        WHERE month = %s
        AND pip_enhanced_daily_living IS NOT NULL
        AND pip_total_claimants IS NOT NULL
        AND pip_enhanced_daily_living > pip_total_claimants
    """, (month_label,))
    inconsistent = cur.fetchone()[0]
    check3 = inconsistent == 0
    results["consistency"] = {"pass": check3, "violations": inconsistent}
    print(f"  3. Consistency: {'PASS' if check3 else 'FAIL'} ({inconsistent} violations)")

    # 4. Range
    cur.execute("""
        SELECT count(*) FROM la_pip_claimants
        WHERE month = %s AND (pip_total_claimants < 0 OR pip_enhanced_daily_living < 0)
    """, (month_label,))
    negatives = cur.fetchone()[0]
    cur.execute(
        "SELECT sum(pip_total_claimants) FROM la_pip_claimants WHERE month = %s",
        (month_label,),
    )
    national_total = cur.fetchone()[0] or 0
    plausible = national_total > 100000
    check4 = negatives == 0 and plausible
    results["range"] = {
        "pass": check4, "negatives": negatives,
        "national_total": int(national_total), "plausible": plausible,
    }
    print(f"  4. Range: {'PASS' if check4 else 'FAIL'} "
          f"(negatives={negatives}, national_total={national_total:,.0f})")

    # 5. Independent spot check
    print("  5. Spot check...")
    cur.execute("""
        SELECT lad24cd, pip_total_claimants, pip_enhanced_daily_living
        FROM la_pip_claimants
        WHERE month = %s AND pip_total_claimants IS NOT NULL
        ORDER BY pip_total_claimants
    """, (month_label,))
    all_rows = cur.fetchall()
    indices = [0, len(all_rows) // 2, len(all_rows) - 1] if len(all_rows) >= 3 else list(range(len(all_rows)))

    spot_ok = True
    spot_details = []
    lad_to_uris = geo["lad_to_uris"]
    for idx in indices:
        lad, loaded_total, loaded_enhanced = all_rows[idx]
        uris = lad_to_uris.get(lad, [])
        if not uris:
            spot_details.append({"lad": lad, "match": False, "reason": "no URI"})
            spot_ok = False
            continue

        q_spot = {
            "database": discovery["database"]["id"],
            "measures": [discovery["measure"]["id"]],
            "recodes": {
                discovery["geography_field_id"]: {
                    "map": [[u] for u in uris], "total": False,
                },
                discovery["date_field_id"]: {
                    "map": [[discovery["latest_month"]["id"]]], "total": False,
                },
            },
            "dimensions": [
                [discovery["geography_field_id"]],
                [discovery["date_field_id"]],
            ],
        }
        resp = api_post("/table", q_spot)
        cubes = resp["cubes"]
        cube_key = list(cubes.keys())[0]
        vals = cubes[cube_key]["values"]
        fresh_total = 0
        for v_row in vals:
            v = v_row
            while isinstance(v, list):
                v = v[0]
            if v is not None:
                fresh_total += v

        match = fresh_total == loaded_total
        spot_details.append({
            "lad": lad, "loaded": loaded_total, "fresh": fresh_total, "match": match,
        })
        if not match:
            spot_ok = False
        print(f"    {lad}: loaded={loaded_total}, fresh={fresh_total} {'OK' if match else 'MISMATCH'}")

    results["spot_check"] = {"pass": spot_ok, "details": spot_details}
    print(f"  5. Spot check: {'PASS' if spot_ok else 'FAIL'}")

    # 6. Idempotency
    print("  6. Idempotency...")
    cur.execute(
        "SELECT count(*) FROM la_pip_claimants WHERE month = %s", (month_label,)
    )
    count_before = cur.fetchone()[0]

    cur.execute("""
        SELECT lad24cd, month, pip_total_claimants, pip_enhanced_daily_living
        FROM la_pip_claimants WHERE month = %s
    """, (month_label,))
    existing = cur.fetchall()
    rebatch = json.dumps([
        {"lad24cd": r[0], "month": r[1],
         "pip_total_claimants": r[2], "pip_enhanced_daily_living": r[3]}
        for r in existing
    ])
    cur.execute("""
        INSERT INTO la_pip_claimants (lad24cd, month, pip_total_claimants, pip_enhanced_daily_living)
        SELECT r.lad24cd, r.month, r.pip_total_claimants, r.pip_enhanced_daily_living
        FROM json_to_recordset(%s::json)
            AS r(lad24cd text, month text, pip_total_claimants int, pip_enhanced_daily_living int)
        ON CONFLICT (lad24cd, month) DO UPDATE SET
            pip_total_claimants = EXCLUDED.pip_total_claimants,
            pip_enhanced_daily_living = EXCLUDED.pip_enhanced_daily_living,
            loaded_at = NOW();
    """, (rebatch,))
    conn.commit()

    cur.execute(
        "SELECT count(*) FROM la_pip_claimants WHERE month = %s", (month_label,)
    )
    count_after = cur.fetchone()[0]
    cur.execute("""
        SELECT count(*) FROM pipeline_run_log
        WHERE agent_name = 'Source 19 - PIP Claimants' AND source_number = '19'
    """)
    log_count = cur.fetchone()[0]

    check6 = count_before == count_after and log_count == 1
    results["idempotency"] = {
        "pass": check6,
        "rows_before": count_before, "rows_after": count_after,
        "log_entries": log_count,
    }
    print(f"  6. Idempotency: {'PASS' if check6 else 'FAIL'} "
          f"(rows: {count_before}->{count_after}, logs: {log_count})")

    all_pass = all(v["pass"] for v in results.values())
    if not all_pass:
        failed = [k for k, v in results.items() if not v["pass"]]
        sys.exit(
            f"HARD STOP: Verification failed: {failed}\n"
            f"{json.dumps(results, indent=2, default=str)}"
        )

    print("  All 6 checks PASSED")
    return results


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    conn = psycopg2.connect(**DB_CFG)
    conn.autocommit = False

    try:
        # Checkpoint: reuse cached discovery if present
        discovery_cache = CACHE_DIR / "discovery.json"
        if discovery_cache.exists():
            print("=== Phase 1: Schema Discovery (from checkpoint) ===")
            discovery = json.loads(discovery_cache.read_text(encoding="utf-8"))
            print(f"  Database: {discovery['database']['label']}")
            print(f"  Measure: {discovery['measure']['id']}")
            print(f"  Latest month: {discovery['latest_month']['label']}")
            print(f"  English LAs: {len(discovery['la_english_members'])}")
        else:
            discovery = discover_schema()
        geo = resolve_geography(discovery, conn)
        rows, annotations = build_and_fetch(discovery, geo)
        rows_written, log_id = create_and_load(conn, rows, discovery, geo, annotations)
        verification = verify(conn, geo, discovery, rows_written, log_id)

        summary = {
            "discovery": {
                k: v for k, v in discovery.items() if k != "la_english_members"
            },
            "geography": {
                k: v for k, v in geo.items()
                if k not in ("lad_to_uris", "all_query_uris")
            },
            "rows_written": rows_written,
            "log_id": log_id,
            "annotations": annotations,
            "verification": verification,
        }
        (CACHE_DIR / "s19_build_summary.json").write_text(
            json.dumps(summary, indent=2, default=str), encoding="utf-8"
        )
        print(f"\n=== S19 PIP Claimants build complete ===")
        return summary
    finally:
        conn.close()


if __name__ == "__main__":
    main()
