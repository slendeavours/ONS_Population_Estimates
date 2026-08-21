#!/usr/bin/env python3
"""
S14 - VOA/DWP LHA Rates Source Builder (Simplified Version)
"""

import os
import sys
import csv
import re
import tempfile
import shutil
from io import StringIO
from pathlib import Path
from urllib.parse import urljoin
from typing import Dict, List, Tuple

import requests
import psycopg2
from psycopg2.extras import execute_values
import geopandas as gpd
import pandas as pd
from shapely.geometry import Point
from math import radians, cos, sin, asin, sqrt

# ============================================================================
# LOAD ENV FILE
# ============================================================================

# This file is 425 lines of top-level script with no main guard, so importing
# it runs the whole S14 build and rebuilds the tables. That happened by accident
# on 2026-08-20 during a routine import check. Wrapping the flow in a function
# would mean re-indenting the entire file; refusing the import is four lines and
# turns a silent rebuild into a loud error.
if __name__ != "__main__":
    raise ImportError(
        "s14_lha_rates_build_v2.py is a script, not a module: importing it "
        "rebuilds the S14 tables. Run it as "
        "`python scripts/s14_lha_rates_build_v2.py`."
    )

# .env was resolved relative to the working directory, so this script found
# credentials only when run from one particular folder, and defaulted the user
# when it did not. _db resolves both candidate locations and refuses to guess.
from _db import ENV, get_conn  # noqa: E402

for _k, _v in ENV.items():
    os.environ.setdefault(_k, _v)

# ============================================================================
# CONFIGURATION
# ============================================================================

DWP_CSV_URL = "https://assets.publishing.service.gov.uk/media/69d654a2e1430e837a86f64a/england-rates-2026-to-2027.csv"
VOA_BRMA_PAGE = "https://www.gov.uk/government/publications/broad-rental-market-area-boundary-layer-for-geographical-information-system-gis-applicable-may-2020"

DB_HOST = (os.getenv('PG_HOST') or 'localhost').replace('postgres', 'localhost')
DB_PORT = int(os.getenv('PG_PORT', 5432))
DB_NAME = os.getenv('PG_DATABASE', 'exempt_pipeline')
DB_USER = os.getenv('PG_USER', 'pipeline_user')
DB_PASSWORD = os.getenv('PG_PASSWORD')

print(f"\nDatabase: {DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME}")

# ============================================================================
# UTILITIES
# ============================================================================

def get_db_connection():
    return get_conn()

def haversine_km(lon1, lat1, lon2, lat2):
    lon1, lat1, lon2, lat2 = map(radians, [lon1, lat1, lon2, lat2])
    dlon, dlat = lon2 - lon1, lat2 - lat1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    return 2 * 6371 * asin(sqrt(a))

# ============================================================================
# TASK 1: DOWNLOAD AND PARSE CSV
# ============================================================================

print("\n=== TASK 1: Parse DWP LHA CSV ===\n")

resp = requests.get(DWP_CSV_URL, timeout=10)
resp.raise_for_status()

# Parse CSV
lines = resp.text.strip().split('\n')
print(f"Total lines: {len(lines)}")
print(f"Line 0 (headers): {lines[0][:60]}")

# Row 0 is the column header row; data starts at row 1. Read as "row 0 title,
# row 1 headers, data from row 2" until 2026-08-20, which silently dropped the
# first BRMA alphabetically and loaded 151 of 152.
reader = csv.reader(lines[1:])

brma_rates = []
for row in reader:
    if not row or not row[0].strip():
        continue

    brma_name = row[0].strip()
    try:
        # Parse money values: remove £ and commas, convert to float
        sar = float(row[1].replace('£', '').replace(',', '').strip())
        bed1 = float(row[2].replace('£', '').replace(',', '').strip())
        bed2 = float(row[3].replace('£', '').replace(',', '').strip())
        bed3 = float(row[4].replace('£', '').replace(',', '').strip())
        bed4 = float(row[5].replace('£', '').replace(',', '').strip())
    except (ValueError, IndexError) as e:
        continue

    brma_rates.append({
        'brma_name': brma_name,
        'sar_monthly': sar,
        'one_bed_monthly': bed1,
        'two_bed_monthly': bed2,
        'three_bed_monthly': bed3,
        'four_bed_monthly': bed4,
        'sar_weekly': round(sar * 12 / 52, 2),
        'one_bed_weekly': round(bed1 * 12 / 52, 2),
        'two_bed_weekly': round(bed2 * 12 / 52, 2),
        'three_bed_weekly': round(bed3 * 12 / 52, 2),
        'four_bed_weekly': round(bed4 * 12 / 52, 2),
    })

print(f"\nParsed {len(brma_rates)} BRMAs\n")
for row in brma_rates[:3]:
    print(f"  {row['brma_name']:30} SAR: GBP{row['sar_weekly']:7.2f}/week")

bham = [r for r in brma_rates if 'Birmingham' in r['brma_name']]
if bham:
    print(f"\nBirmingham SAR: GBP{bham[0]['sar_weekly']:.2f}/week (expected GBP78.83)")
else:
    print("\nWARNING: Birmingham not found in rates")

# ============================================================================
# TASK 2: DOWNLOAD BRMA BOUNDARIES
# ============================================================================

print("\n=== TASK 2a: Fetch BRMA Boundaries ===\n")

html = requests.get(VOA_BRMA_PAGE).text
match = re.search(r'href="([^"]*\.zip)"', html)
if not match:
    print("ERROR: Cannot find ZIP link")
    sys.exit(1)

zip_url = match.group(1)
if not zip_url.startswith('http'):
    zip_url = urljoin(VOA_BRMA_PAGE, zip_url)

print(f"Downloading from: {zip_url}")

with tempfile.TemporaryDirectory() as tmpdir:
    zip_path = Path(tmpdir) / "brma.zip"
    zip_path.write_bytes(requests.get(zip_url).content)
    shutil.unpack_archive(zip_path, tmpdir)

    gml_files = list(Path(tmpdir).glob("**/*.gml"))
    shp_files = list(Path(tmpdir).glob("**/*.shp"))

    if gml_files:
        gdf = gpd.read_file(gml_files[0])
        print(f"Loaded GML with {len(gdf)} geometries")
    elif shp_files:
        gdf = gpd.read_file(shp_files[0])
        print(f"Loaded shapefile with {len(gdf)} geometries")
    else:
        print("ERROR: No GML or shapefile found")
        sys.exit(1)

    # Fix CRS
    if gdf.crs is None:
        print("Setting CRS to EPSG:27700 (British National Grid)...")
        gdf = gdf.set_crs('EPSG:27700')

    if gdf.crs != 'EPSG:4326':
        print(f"Reprojecting to WGS84...")
        gdf = gdf.to_crs('EPSG:4326')

    # Find BRMA name column
    brma_col = 'Name'
    for col in gdf.columns:
        if 'name' in col.lower() or 'brma' in col.lower():
            brma_col = col
            break

    print(f"Using column: {brma_col}")
    gdf = gdf.rename(columns={brma_col: 'brma_name'}).copy()

# ============================================================================
# TASK 2b: BUILD MAPPING AND VERIFY
# ============================================================================

print("\n=== TASK 2b: Build LA<->BRMA Mapping ===\n")

conn = get_db_connection()
cur = conn.cursor()
cur.execute("SELECT lad24cd, lad24nm, longitude, latitude FROM la_boundaries")
la_rows = cur.fetchall()
cur.close()
conn.close()

print(f"Loaded {len(la_rows)} LA centroids")

la_gdf = gpd.GeoDataFrame(
    [{'lad24cd': r[0], 'la_name': r[1], 'geometry': Point(float(r[2]), float(r[3]))}
     for r in la_rows],
    crs='EPSG:4326'
)

# Spatial join
mapping = gpd.sjoin(la_gdf, gdf[['geometry', 'brma_name']], how='left', predicate='within')

# Fallback nearest
for idx in mapping[mapping['brma_name'].isna()].index:
    la_point = mapping.loc[idx, 'geometry']
    nearest_idx = gdf.geometry.distance(la_point).idxmin()
    mapping.loc[idx, 'brma_name'] = gdf.loc[nearest_idx, 'brma_name']
    mapping.loc[idx, 'mapping_method'] = 'nearest_fallback'

mapping['mapping_method'] = mapping.get('mapping_method', 'centroid_spatial_join')
mapping_df = mapping[['lad24cd', 'la_name', 'brma_name', 'mapping_method']].copy()

print(f"Mapped {len(mapping_df)} LAs to {mapping_df['brma_name'].nunique()} BRMAs\n")

# Verification
print("=== Verification ===")

# Check London
london_codes = ['E0900' + f'{i:02d}' for i in range(1, 34)]
london_mapped = mapping_df[mapping_df['lad24cd'].isin(london_codes)]
london_bad = london_mapped[~london_mapped['brma_name'].str.contains('London')]
if len(london_bad) > 0:
    print(f"ERROR: {len(london_bad)} London boroughs mapped outside London")
    sys.exit(1)
else:
    print(f"PASS: All {len(london_mapped)} London boroughs mapped correctly")

# Check coverage
if len(mapping_df) != 296:
    print(f"ERROR: Only {len(mapping_df)}/296 LAs mapped")
    sys.exit(1)
else:
    print(f"PASS: All 296 LAs mapped")

# Check rates
bad_rates = [r for r in brma_rates if r['sar_weekly'] < 50 or r['sar_weekly'] > 250]
if bad_rates:
    print(f"ERROR: {len(bad_rates)} rates out of range")
    sys.exit(1)
else:
    print(f"PASS: All rates in range (GBP50-250)")

print("\nVERIFICATION PASSED")

# ============================================================================
# TASK 3-4: CREATE AND LOAD
# ============================================================================

print("\n=== TASK 3: Create Tables ===\n")

conn = get_db_connection()
cur = conn.cursor()

# Drop existing tables to start fresh
cur.execute("DROP TABLE IF EXISTS la_brma_mapping CASCADE")
cur.execute("DROP TABLE IF EXISTS brma_lha_rates CASCADE")

cur.execute("""
    CREATE TABLE la_brma_mapping (
        lad24cd VARCHAR(9) PRIMARY KEY,
        la_name VARCHAR(100),
        brma_name VARCHAR(100) NOT NULL,
        brma_secondary VARCHAR(100),
        mapping_method TEXT DEFAULT 'centroid_spatial_join',
        source TEXT,
        loaded_at TIMESTAMPTZ DEFAULT NOW()
    )
""")

cur.execute("""
    CREATE TABLE brma_lha_rates (
        brma_name VARCHAR(100) NOT NULL,
        financial_year VARCHAR(7) NOT NULL,
        sar_weekly NUMERIC(8,2),
        one_bed_weekly NUMERIC(8,2),
        two_bed_weekly NUMERIC(8,2),
        three_bed_weekly NUMERIC(8,2),
        four_bed_weekly NUMERIC(8,2),
        sar_monthly NUMERIC(8,2),
        one_bed_monthly NUMERIC(8,2),
        two_bed_monthly NUMERIC(8,2),
        three_bed_monthly NUMERIC(8,2),
        four_bed_monthly NUMERIC(8,2),
        source TEXT,
        loaded_at TIMESTAMPTZ DEFAULT NOW(),
        PRIMARY KEY (brma_name, financial_year)
    )
""")

conn.commit()
print("Tables created (fresh)")

print("\n=== TASK 4: Load Data ===\n")

# Upsert mapping
mapping_tuples = [
    (row['lad24cd'], row['la_name'], row['brma_name'], None,
     row.get('mapping_method', 'centroid_spatial_join'), 'VOA BRMA May 2020 x la_boundaries centroids')
    for _, row in mapping_df.iterrows()
]

execute_values(cur, """
    INSERT INTO la_brma_mapping (lad24cd, la_name, brma_name, brma_secondary, mapping_method, source)
    VALUES %s ON CONFLICT (lad24cd) DO UPDATE SET
        la_name = EXCLUDED.la_name, brma_name = EXCLUDED.brma_name,
        mapping_method = EXCLUDED.mapping_method, loaded_at = NOW()
""", mapping_tuples, page_size=100)

# Upsert rates
rate_tuples = [
    (r['brma_name'], '2026-27',
     r['sar_weekly'], r['one_bed_weekly'], r['two_bed_weekly'],
     r['three_bed_weekly'], r['four_bed_weekly'],
     r['sar_monthly'], r['one_bed_monthly'], r['two_bed_monthly'],
     r['three_bed_monthly'], r['four_bed_monthly'],
     'DWP UC LHA rates 2026-27, https://www.gov.uk/government/publications/universal-credit-local-housing-allowance-rates-2026-to-2027')
    for r in brma_rates
]

execute_values(cur, """
    INSERT INTO brma_lha_rates
    (brma_name, financial_year, sar_weekly, one_bed_weekly, two_bed_weekly,
     three_bed_weekly, four_bed_weekly, sar_monthly, one_bed_monthly, two_bed_monthly,
     three_bed_monthly, four_bed_monthly, source)
    VALUES %s ON CONFLICT (brma_name, financial_year) DO UPDATE SET
        sar_weekly = EXCLUDED.sar_weekly, one_bed_weekly = EXCLUDED.one_bed_weekly,
        two_bed_weekly = EXCLUDED.two_bed_weekly, three_bed_weekly = EXCLUDED.three_bed_weekly,
        four_bed_weekly = EXCLUDED.four_bed_weekly, sar_monthly = EXCLUDED.sar_monthly,
        one_bed_monthly = EXCLUDED.one_bed_monthly, two_bed_monthly = EXCLUDED.two_bed_monthly,
        three_bed_monthly = EXCLUDED.three_bed_monthly, four_bed_monthly = EXCLUDED.four_bed_monthly,
        loaded_at = NOW()
""", rate_tuples, page_size=100)

conn.commit()
print(f"Loaded {len(mapping_tuples)} LA mappings")
print(f"Loaded {len(rate_tuples)} BRMA rates")

# ============================================================================
# TASK 5: ADD LHA COLUMNS
# ============================================================================

print("\n=== TASK 5: Add LHA Columns ===\n")

cur.execute("""
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
        WHERE table_name = 'staging_la_signals' AND column_name = 'lha_brma_name') THEN
        ALTER TABLE staging_la_signals ADD COLUMN lha_brma_name VARCHAR(100);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
        WHERE table_name = 'staging_la_signals' AND column_name = 'lha_sar_weekly') THEN
        ALTER TABLE staging_la_signals ADD COLUMN lha_sar_weekly NUMERIC(8,2);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
        WHERE table_name = 'staging_la_signals' AND column_name = 'lha_1bed_weekly') THEN
        ALTER TABLE staging_la_signals ADD COLUMN lha_1bed_weekly NUMERIC(8,2);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
        WHERE table_name = 'staging_la_signals' AND column_name = 'lha_2bed_weekly') THEN
        ALTER TABLE staging_la_signals ADD COLUMN lha_2bed_weekly NUMERIC(8,2);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
        WHERE table_name = 'staging_la_signals' AND column_name = 'lha_3bed_weekly') THEN
        ALTER TABLE staging_la_signals ADD COLUMN lha_3bed_weekly NUMERIC(8,2);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
        WHERE table_name = 'staging_la_signals' AND column_name = 'lha_4bed_weekly') THEN
        ALTER TABLE staging_la_signals ADD COLUMN lha_4bed_weekly NUMERIC(8,2);
    END IF;
END $$;
""")

conn.commit()
print("LHA columns added")

# ============================================================================
# TASK 6: LOG PIPELINE RUN
# ============================================================================

print("\n=== TASK 6: Log Pipeline Run ===\n")

cur.execute("""
    INSERT INTO pipeline_run_log (agent_name, source_number, rows_written, started_at, completed_at, status, notes)
    VALUES (%s, %s, %s, NOW(), NOW(), %s, %s)
""", ('Source 14 - LHA Rates', 14, len(mapping_tuples) + len(rate_tuples), 'success',
      'DWP UC LHA rates 2026-27 (frozen at 2024-25 levels). BRMA-to-LA mapping via centroid spatial join.'))

conn.commit()
print("Pipeline run logged")

# ============================================================================
# TASK 7: VERIFICATION QUERIES
# ============================================================================

print("\n=== TASK 7: Verification Queries ===\n")

cur.execute("SELECT COUNT(*) FROM brma_lha_rates WHERE financial_year = '2026-27'")
print(f"BRMAs loaded: {cur.fetchone()[0]}")

cur.execute("""
    SELECT brma_name, sar_weekly, one_bed_weekly, four_bed_weekly
    FROM brma_lha_rates WHERE financial_year = '2026-27' ORDER BY brma_name LIMIT 5
""")
for brma, sar, bed1, bed4 in cur.fetchall():
    print(f"  {brma:30} SAR: GBP{sar:7.2f}  1bed: GBP{bed1:7.2f}  4bed: GBP{bed4:7.2f}")

cur.execute("SELECT COUNT(*) FROM la_brma_mapping")
print(f"\nLAs mapped: {cur.fetchone()[0]}")

cur.execute("""
    SELECT m.la_name, m.brma_name, r.sar_weekly
    FROM la_brma_mapping m
    JOIN brma_lha_rates r ON r.brma_name = m.brma_name AND r.financial_year = '2026-27'
    WHERE m.la_name IN ('Birmingham', 'Liverpool', 'Nottingham', 'Manchester', 'Blackpool')
    ORDER BY m.la_name
""")
print("\nTarget market rates:")
for la, brma, sar in cur.fetchall():
    print(f"  {la:20} -> {brma:30} SAR: GBP{sar:7.2f}")

cur.close()
conn.close()

print("\n" + "="*70)
print("S14 BUILD COMPLETE")
print("="*70)
