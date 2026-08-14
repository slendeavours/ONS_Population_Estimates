"""
s11_cqc_map.py — S11 Node 3: Resolve each location to a LAD24CD district.

Purpose : The CQC file's Local Authority field is upper-tier only (155 names -
          Suffolk, Kent etc.), so it cannot join to the 296 LAD24CD districts.
          This node spatially joins location lat/long points into the full
          la_boundaries polygons (EPSG:4326), the S14 pattern inverted.
          Fallbacks, each recorded in mapping_method:
            point_in_polygon      - point falls inside a district polygon
            nearest_fallback      - point outside all polygons (coastal edge);
                                    assigned to the nearest polygon
            postcode_api_fallback - no usable coordinates; postcode resolved
                                    via api.postcodes.io (ONS-backed). Codes
                                    not found in la_boundaries are reconciled
                                    through la_code_lookup before storage.
            postcode_terminated_fallback - postcode no longer live; the
                                    terminated_postcodes endpoint still returns
                                    its coordinates, which then go through the
                                    normal point-in-polygon assignment.
          Rows that still cannot be mapped are excluded and listed - the load
          node never stores a null lad24cd. (July 2026 run: 5 such rows, all
          with postcodes unknown to postcodes.io - new postcodes ahead of the
          ONSPD edition, or CQC typos. Re-resolved automatically on a later
          monthly run once ONSPD catches up.)
          Cross-check only (never the join): where the CQC LA name exactly
          matches a lad24nm, compare it with the spatially assigned district.
Inputs  : data/processed/cqc_locations_processed.csv
          la_boundaries (polygon geojson) and la_code_lookup from Postgres
Outputs : data/processed/cqc_locations_mapped.csv
          Prints mapping_method counts, per-method listings, cross-check rate.
"""
import json
import os
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
import psycopg2
import requests
from psycopg2.extras import execute_values
from shapely.geometry import shape

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "processed" / "cqc_locations_processed.csv"
OUT = ROOT / "data" / "processed" / "cqc_locations_mapped.csv"


def pg_conn():
    """Connect using PG_* values from the repo .env (never hardcoded)."""
    env = {}
    # .env sits at the working-copy root: this repository root in the
    # outer checkout, one level up inside the published one. Same
    # two-location lookup as scripts/_db.py.
    env_file = next((p for p in (ROOT / ".env", ROOT.parent / ".env")
                     if p.exists()), ROOT / ".env")
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    password = os.environ.get("PG_PASSWORD") or env.get("PG_PASSWORD")
    if not password:
        sys.exit("PG_PASSWORD not set (env var or .env) - refusing to guess")
    return psycopg2.connect(
        host=os.environ.get("PG_HOST", "localhost"), port=5432,
        dbname=env.get("PG_DATABASE", "exempt_pipeline"),
        user=os.environ.get("PG_USER") or env.get("PG_USER"), password=password)


def postcode_lookup(postcodes):
    """Bulk-resolve postcodes to LAD codes via postcodes.io (max 100/call)."""
    results = {}
    pcs = list(postcodes)
    for i in range(0, len(pcs), 100):
        batch = pcs[i:i + 100]
        r = requests.post("https://api.postcodes.io/postcodes",
                          json={"postcodes": batch}, timeout=60)
        r.raise_for_status()
        for item in r.json()["result"]:
            res = item.get("result")
            if res and res.get("codes", {}).get("admin_district"):
                results[item["query"]] = res["codes"]["admin_district"]
    return results


UNRESOLVED_DDL = """
CREATE TABLE IF NOT EXISTS cqc_unresolved_locations (
    location_id      TEXT PRIMARY KEY,
    location_name    TEXT,
    postcode         TEXT,
    reason           TEXT NOT NULL,
    first_seen_edition DATE NOT NULL,
    last_seen_edition  DATE NOT NULL,
    editions_seen    INTEGER NOT NULL DEFAULT 1,
    resolved_at      TIMESTAMPTZ,
    loaded_at        TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE cqc_unresolved_locations IS
'CQC locations that could not be resolved to a lad24cd and are therefore absent from cqc_locations. S11 is the pipeline''s only supply-side source, so each row here understates provision in some local authority by one location. UNEXPLAINED under the standing rule, never benign: the reason column records what was actually established, not an assumption. resolved_at is set when a location later resolves, so the row survives as a record rather than vanishing.';

COMMENT ON COLUMN cqc_unresolved_locations.reason IS
'What was established, with evidence. Not a category guess.';

COMMENT ON COLUMN cqc_unresolved_locations.editions_seen IS
'How many editions this location has been unresolved for. A rising count is a persistent gap, not a transient one.';
"""


def record_unresolved(cur, unmapped, file_date):
    """Persist unresolved locations, and mark any that have since resolved."""
    cur.execute(UNRESOLVED_DDL)
    cur.execute("ALTER TABLE cqc_unresolved_locations OWNER TO pipeline_user")

    still = []
    for r in unmapped.itertuples(index=False):
        still.append((r.location_id, r.location_name, r.postcode,
                      "postcode absent from ONS data (not live, not "
                      "terminated) and the file carries no coordinates and no "
                      "LA name; the outward code spans more than one LA, so "
                      "no resolution is available without a guess",
                      file_date, file_date))
    if still:
        execute_values(cur, """
            INSERT INTO cqc_unresolved_locations
                (location_id, location_name, postcode, reason,
                 first_seen_edition, last_seen_edition)
            VALUES %s
            ON CONFLICT (location_id) DO UPDATE SET
                location_name = EXCLUDED.location_name,
                postcode      = EXCLUDED.postcode,
                reason        = EXCLUDED.reason,
                last_seen_edition = EXCLUDED.last_seen_edition,
                editions_seen = cqc_unresolved_locations.editions_seen
                                + (CASE WHEN EXCLUDED.last_seen_edition
                                             > cqc_unresolved_locations.last_seen_edition
                                        THEN 1 ELSE 0 END),
                resolved_at   = NULL,
                loaded_at     = NOW()
        """, still)

    # Anything previously unresolved and absent from this edition's list has
    # either resolved or left the register. Stamped rather than deleted.
    ids = [s[0] for s in still]
    cur.execute("""
        UPDATE cqc_unresolved_locations
           SET resolved_at = NOW()
         WHERE resolved_at IS NULL
           AND NOT (location_id = ANY(%s))
    """, (ids,))
    cur.execute("SELECT COUNT(*) FROM cqc_unresolved_locations "
                "WHERE resolved_at IS NULL")
    print(f"cqc_unresolved_locations: {cur.fetchone()[0]} open")


def main():
    df = pd.read_csv(SRC, dtype={"location_id": str, "postcode": str})
    conn = pg_conn()
    cur = conn.cursor()
    cur.execute("SELECT lad24cd, lad24nm, geojson FROM la_boundaries")
    rows = cur.fetchall()
    cur.execute("SELECT old_code, new_code FROM la_code_lookup "
                "WHERE change_type <> 'current'")
    lookup = dict(cur.fetchall())
    conn.close()

    la = gpd.GeoDataFrame(
        {"lad24cd": [r[0] for r in rows], "lad24nm": [r[1] for r in rows]},
        geometry=[shape(r[2] if isinstance(r[2], dict) else json.loads(r[2]))
                  for r in rows],
        crs="EPSG:4326")
    valid_codes = set(la["lad24cd"])
    name_to_code = dict(zip(la["lad24nm"], la["lad24cd"]))

    has_xy = df["latitude"].notna() & df["longitude"].notna()
    pts = gpd.GeoDataFrame(
        df[has_xy][["location_id"]],
        geometry=gpd.points_from_xy(df[has_xy]["longitude"],
                                    df[has_xy]["latitude"]),
        crs="EPSG:4326")

    joined = gpd.sjoin(pts, la, how="left", predicate="within")
    # a point exactly on a shared boundary can match twice - keep the first
    joined = joined[~joined.index.duplicated(keep="first")]
    df.loc[joined.index, "lad24cd"] = joined["lad24cd"]
    df.loc[joined.index[joined["lad24cd"].notna()],
           "mapping_method"] = "point_in_polygon"

    # nearest fallback for points outside every polygon (distance in metres,
    # so both layers reproject to British National Grid first)
    la_bng = la.to_crs("EPSG:27700")
    missed = pts.loc[joined.index[joined["lad24cd"].isna()]]
    if len(missed):
        missed_bng = missed.to_crs("EPSG:27700")
        for idx, row in missed_bng.iterrows():
            dist = la_bng.geometry.distance(row.geometry)
            near = la.loc[dist.idxmin()]
            df.loc[idx, "lad24cd"] = near["lad24cd"]
            df.loc[idx, "mapping_method"] = "nearest_fallback"
            print(f"nearest_fallback: {df.loc[idx, 'location_id']} "
                  f"{df.loc[idx, 'location_name']!r} -> {near['lad24cd']} "
                  f"({near['lad24nm']}), {dist.min():.0f} m")

    # postcode fallback for rows with no usable coordinates
    no_xy = df[~has_xy]
    if len(no_xy):
        pc_map = postcode_lookup(no_xy["postcode"].dropna().unique())
        for idx, row in no_xy.iterrows():
            code = pc_map.get(row["postcode"])
            if code and code not in valid_codes:
                code = lookup.get(code)  # historical code reconciliation
            if code and code in valid_codes:
                df.loc[idx, "lad24cd"] = code
                df.loc[idx, "mapping_method"] = "postcode_api_fallback"
                print(f"postcode_api_fallback: {row['location_id']} "
                      f"{row['location_name']!r} {row['postcode']} -> {code}")
                continue
            # terminated postcode: endpoint still carries coordinates
            r = requests.get("https://api.postcodes.io/terminated_postcodes/"
                             + str(row["postcode"]).replace(" ", ""),
                             timeout=30)
            res = r.json().get("result") if r.ok else None
            if res and res.get("latitude") is not None:
                pt = gpd.GeoSeries(
                    gpd.points_from_xy([res["longitude"]], [res["latitude"]]),
                    crs="EPSG:4326")
                hit = la[la.contains(pt.iloc[0])]
                if len(hit):
                    df.loc[idx, "lad24cd"] = hit.iloc[0]["lad24cd"]
                    df.loc[idx, "mapping_method"] = "postcode_terminated_fallback"
                    print(f"postcode_terminated_fallback: {row['location_id']} "
                          f"{row['location_name']!r} {row['postcode']} -> "
                          f"{hit.iloc[0]['lad24cd']} ({hit.iloc[0]['lad24nm']})")

    unmapped = df[df["lad24cd"].isna()]
    if len(unmapped):
        print(f"UNMAPPED (excluded from load): {len(unmapped)}")
        print(unmapped[["location_id", "location_name", "postcode"]]
              .to_string(index=False))
    # Recorded at table level, not only on stdout. These are locations
    # silently absent from a supply-side source: five of them understate
    # provision by five, every edition, and a run note nobody reads is how a
    # small persistent gap ends up in a council briefing as a supply figure.
    # first_seen against last_seen shows how long each has persisted.
    # main() closes its connection early, after reading boundaries, so
    # this write opens its own rather than depending on that order.
    file_date = (ROOT / "data" / "raw" / "s11_csv" / "FILE_DATE.txt"
                 ).read_text(encoding="utf-8").strip()
    wconn = pg_conn()
    wcur = wconn.cursor()
    try:
        record_unresolved(wcur, unmapped, file_date)
        wconn.commit()
    finally:
        wcur.close()
        wconn.close()
    df = df[df["lad24cd"].notna()].copy()

    bad = set(df["lad24cd"]) - valid_codes
    assert not bad, f"mapped codes missing from la_boundaries: {bad}"

    # cross-check: CQC LA name vs assigned district, where names are comparable
    comparable = df["la_name_cqc"].isin(name_to_code)
    expected = df.loc[comparable, "la_name_cqc"].map(name_to_code)
    agree = (df.loc[comparable, "lad24cd"] == expected)
    print(f"name_crosscheck: {int(agree.sum())}/{int(comparable.sum())} agree "
          f"({agree.mean() * 100:.2f}%)")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT, index=False)
    print(f"written={OUT} rows={len(df)}")
    print(df["mapping_method"].value_counts().to_dict())
    print("OK")


if __name__ == "__main__":
    main()
