"""Node 9 equivalent: export the map data files from the pipeline database.

There is no Node 9 in n8n. Workflow 1 has eight nodes and ends at "Mark Run
Complete"; no workflow in n8ndb builds a FeatureCollection. The published
files were produced by hand, which is why staging_la_signals_latest.json and
la_boundaries.geojson had drifted apart: the signals file carried
hb_sa_claimants_latest and the GeoJSON did not.

This script reproduces the export deterministically:

  data/signals/staging_la_signals_latest.json   all signal columns, no geometry
  data/boundaries/la_boundaries.geojson         geometry + the same properties
  data/signals/latest.json                      run metadata

hb_sa_claimants_latest is NOT a staging_la_signals column. It is joined from
la_hb_accom_type_caseload (S8b), SA category, latest available month. That
join is the field's only provenance and is why a naive export dropped it.

Writes files only. Adds no map layer; index.html decides what renders.
"""

import datetime
import json
import os
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
REPO = _HERE.parent
load_dotenv(REPO / ".env")
load_dotenv(REPO.parent / ".env")


def _require_env(name):
    value = os.environ.get(name)
    if not value:
        sys.exit(f"HARD STOP: {name} is not set. Set it in the environment "
                 f"or .env. This script will not guess a credential.")
    return value


DB_CFG = dict(
    host=(os.environ.get("PG_HOST") or "localhost").replace("postgres",
                                                            "localhost"),
    port=int(os.environ.get("PG_PORT", "5432")),
    dbname=os.environ.get("PG_DATABASE", "exempt_pipeline"),
    user=_require_env("PG_USER"),
    password=_require_env("PG_PASSWORD"),
)

# Not signal data; excluded from the exported properties.
NON_SIGNAL = {"run_id"}


def main():
    conn = psycopg2.connect(**DB_CFG)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT MAX(run_id) AS r FROM staging_la_signals")
    run_id = cur.fetchone()["r"]

    cur.execute("""
        SELECT column_name FROM information_schema.columns
         WHERE table_schema='public' AND table_name='staging_la_signals'
         ORDER BY ordinal_position
    """)
    cols = [r["column_name"] for r in cur.fetchall()]
    signal_cols = [c for c in cols if c not in NON_SIGNAL]

    # hb_sa_claimants_latest is not a staging column; join it from S8b.
    cur.execute("""
        SELECT s.*, h.claimants AS hb_sa_claimants_latest
          FROM staging_la_signals s
          LEFT JOIN (
                SELECT lad24cd, claimants
                  FROM la_hb_accom_type_caseload
                 WHERE accom_type = 'SA'
                   AND month = (SELECT MAX(month)
                                  FROM la_hb_accom_type_caseload
                                 WHERE accom_type = 'SA')
          ) h ON h.lad24cd = s.lad24cd
         WHERE s.run_id = %s
         ORDER BY s.la_name
    """, (run_id,))
    rows = cur.fetchall()

    def props(r):
        out = {}
        for c in signal_cols:
            v = r[c]
            if isinstance(v, datetime.date):
                v = v.isoformat()
            elif hasattr(v, "quantize"):          # numeric -> float
                v = float(v)
            out[c] = v
        out["hb_sa_claimants_latest"] = r["hb_sa_claimants_latest"]
        return out

    generated = datetime.datetime.now(datetime.timezone.utc).isoformat()
    meta = {"generated_at": generated, "run_id": str(run_id),
            "feature_count": str(len(rows)), "source": "exempt_pipeline"}

    signals_path = REPO / "data" / "signals" / "staging_la_signals_latest.json"
    signals_path.write_text(json.dumps(
        {"metadata": meta, "signals": [props(r) for r in rows]},
        ensure_ascii=False, indent=None), encoding="utf-8")

    cur.execute("""
        SELECT lad24cd, geojson FROM la_boundaries
         WHERE geojson IS NOT NULL ORDER BY lad24cd
    """)
    geom = {r["lad24cd"]: r["geojson"] for r in cur.fetchall()}
    by_code = {r["lad24cd"]: r for r in rows}

    features = []
    for code in sorted(geom):
        r = by_code.get(code)
        if r is None:
            continue
        features.append({"type": "Feature", "geometry": geom[code],
                         "properties": props(r)})

    gj_meta = dict(meta, projection="WGS84")
    gj_path = REPO / "data" / "boundaries" / "la_boundaries.geojson"
    gj_path.write_text(json.dumps(
        {"type": "FeatureCollection", "metadata": gj_meta,
         "features": features}, ensure_ascii=False), encoding="utf-8")

    latest_path = REPO / "data" / "signals" / "latest.json"
    latest_path.write_text(json.dumps(
        {"generated_at": generated, "run_id": str(run_id),
         "feature_count": len(features)}, indent=2), encoding="utf-8")

    print(f"run_id                 : {run_id}")
    print(f"signal columns exported: {len(signal_cols) + 1} "
          f"({len(signal_cols)} from staging_la_signals + "
          f"hb_sa_claimants_latest joined from S8b)")
    print(f"signals rows           : {len(rows)}")
    print(f"geojson features       : {len(features)}")
    for p in (signals_path, gj_path, latest_path):
        print(f"  {p.relative_to(REPO)}  {p.stat().st_size:,} bytes")

    missing = [c for c in ("drd_bed_days_lost", "drd_pct_delayed_1plus_days",
                           "crfd_days", "pip_total_claimants",
                           "pip_enhanced_daily_living", "pip_rate_per_1000",
                           "hb_sa_claimants_latest",
                           "ctb_total_dwellings", "ctb_empty_6m_plus",
                           "ctb_empty_homes_premium", "ctb_second_homes",
                           "ctb_lte_rate_pct")
               if c not in features[0]["properties"]]
    if missing:
        sys.exit(f"HARD STOP: expected columns absent from export: {missing}")
    print("\nAll S9, PIP, HB and S22 Council Taxbase columns present in "
          "both files.")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
