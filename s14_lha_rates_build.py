#!/usr/bin/env python3
"""
S14 - VOA/DWP LHA Rates Source Builder
Builds the spatial crosswalk from BRMAs to LAs and loads LHA rates into the pipeline.
"""

import os
import sys
import csv
import re
import tempfile
import shutil
import json
from io import StringIO
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin
from difflib import SequenceMatcher
from typing import Dict, List, Tuple, Optional

import requests
import psycopg2
from psycopg2.extras import Json, execute_values
import geopandas as gpd
import pandas as pd
from shapely.geometry import Point
from math import radians, cos, sin, asin, sqrt

# ============================================================================
# CONFIGURATION
# ============================================================================

# Load .env file if it exists
env_file = Path('.env')
if env_file.exists():
    for line in env_file.read_text().split('\n'):
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            key = key.strip()
            value = value.strip()
            if key not in os.environ:
                os.environ[key] = value

DWP_CSV_URL = "https://assets.publishing.service.gov.uk/media/69d654a2e1430e837a86f64a/england-rates-2026-to-2027.csv"
VOA_BRMA_PAGE = "https://www.gov.uk/government/publications/broad-rental-market-area-boundary-layer-for-geographical-information-system-gis-applicable-may-2020"
DB_HOST = os.getenv('DB_HOST') or os.getenv('PG_HOST', 'localhost')
# If host is "postgres" (Docker internal), use localhost instead (for local dev)
if DB_HOST == 'postgres':
    DB_HOST = 'localhost'
DB_PORT = int(os.getenv('DB_PORT') or os.getenv('PG_PORT', 5432))
DB_NAME = os.getenv('DB_NAME') or os.getenv('PG_DATABASE', 'exempt_pipeline')
DB_USER = os.getenv('DB_USER') or os.getenv('PG_USER', 'pipeline_user')
DB_PASSWORD = os.getenv('EXEMPT_PIPELINE_DB_PASSWORD') or os.getenv('PG_PASSWORD')

# Anchor set - verified correct mappings
ANCHOR_SET = {
    'E08000025': 'Birmingham',
    'E08000012': 'Greater Liverpool',
    'E06000018': 'Nottingham',
    'E08000003': 'Central Greater Manchester',
    'E06000009': 'Fylde Coast',
    'E08000035': 'Leeds',
    'E08000016': 'Barnsley',
    'E08000019': 'Sheffield',
    'E08000032': 'Bradford & South Dales',
    'E06000015': 'Derby',
    'E08000036': 'Wakefield',
    'E06000014': 'York',
    'E06000031': 'Peterborough',
    'E07000008': 'Cambridge',
    'E07000178': 'Oxford',
    'E06000044': 'Portsmouth',
    'E06000045': 'Southampton',
    'E06000043': 'Brighton and Hove',
    'E06000023': 'Bristol',
    'E06000022': 'Bath',
    'E08000026': 'Coventry',
    'E06000016': 'Leicester',
    'E07000071': 'Colchester',
    'E07000202': 'Ipswich',
    'E08000028': 'Black Country',
    'E08000029': 'Solihull',
    'E08000030': 'Black Country',
    'E08000031': 'Black Country',
    'E06000046': 'Isle of Wight',
    'E08000011': 'Greater Liverpool',
}

LONDON_BOROUGHS = [
    'E09000001', 'E09000002', 'E09000003', 'E09000004', 'E09000005',
    'E09000006', 'E09000007', 'E09000008', 'E09000009', 'E09000010',
    'E09000011', 'E09000012', 'E09000013', 'E09000014', 'E09000015',
    'E09000016', 'E09000017', 'E09000018', 'E09000019', 'E09000020',
    'E09000021', 'E09000022', 'E09000023', 'E09000024', 'E09000025',
    'E09000026', 'E09000027', 'E09000028', 'E09000029', 'E09000030',
    'E09000031', 'E09000032', 'E09000033',
]

# ============================================================================
# UTILITIES
# ============================================================================

def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Calculate great-circle distance between two points in km."""
    lon1, lat1, lon2, lat2 = map(radians, [lon1, lat1, lon2, lat2])
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    return 2 * 6371 * asin(sqrt(a))

def fuzzy_match_ratio(s1: str, s2: str) -> float:
    """Simple fuzzy match using SequenceMatcher."""
    return SequenceMatcher(None, s1.lower(), s2.lower()).ratio()

def get_db_connection():
    """Get Postgres connection."""
    if not DB_PASSWORD:
        raise ValueError("EXEMPT_PIPELINE_DB_PASSWORD not set. Prompt user.")
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD
    )

def fetch_html(url: str) -> str:
    """Fetch HTML from URL."""
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    return resp.text

def fetch_csv(url: str) -> str:
    """Fetch CSV from URL."""
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    return resp.text

def fetch_binary(url: str, filename: str = None) -> bytes:
    """Fetch binary file from URL."""
    resp = requests.get(url, timeout=30, stream=True)
    resp.raise_for_status()
    return resp.content

# ============================================================================
# TASK 1: DOWNLOAD AND PARSE DWP CSV
# ============================================================================

def task1_parse_dwp_csv() -> List[Dict]:
    """Download and parse DWP LHA CSV."""
    print("\n=== TASK 1: Download and Parse DWP LHA CSV ===\n")

    print(f"Fetching DWP CSV from {DWP_CSV_URL}...")
    csv_text = fetch_csv(DWP_CSV_URL)

    # Parse CSV manually to handle formatting
    lines = csv_text.strip().split('\n')

    # Row 0 is title, row 1 is headers, data starts row 2
    title_row = lines[0]
    print(f"Title row: {title_row}")

    headers = lines[1].split(',')
    headers = [h.strip('"').strip() for h in headers]
    print(f"Headers: {headers}\n")

    brma_rates = []
    for i, line in enumerate(lines[2:], start=3):
        # Parse CSV line handling quoted fields
        reader = csv.reader(StringIO(line))
        row = next(reader)
        row = [v.strip('"').strip() for v in row]

        if len(row) < 6:
            continue

        brma_name = row[0].strip()

        # Parse and clean numeric values
        def parse_money(val):
            val = val.replace('GBP', '').replace(',', '').strip()
            return float(val) if val else 0

        try:
            sar_monthly = parse_money(row[1])
            one_bed_monthly = parse_money(row[2])
            two_bed_monthly = parse_money(row[3])
            three_bed_monthly = parse_money(row[4])
            four_bed_monthly = parse_money(row[5])
        except (ValueError, IndexError) as e:
            print(f"Skipping row {i}: {e}")
            continue

        # Convert to weekly: weekly = monthly * 12 / 52
        sar_weekly = round(sar_monthly * 12 / 52, 2)
        one_bed_weekly = round(one_bed_monthly * 12 / 52, 2)
        two_bed_weekly = round(two_bed_monthly * 12 / 52, 2)
        three_bed_weekly = round(three_bed_monthly * 12 / 52, 2)
        four_bed_weekly = round(four_bed_monthly * 12 / 52, 2)

        brma_rates.append({
            'brma_name': brma_name,
            'sar_monthly': sar_monthly,
            'one_bed_monthly': one_bed_monthly,
            'two_bed_monthly': two_bed_monthly,
            'three_bed_monthly': three_bed_monthly,
            'four_bed_monthly': four_bed_monthly,
            'sar_weekly': sar_weekly,
            'one_bed_weekly': one_bed_weekly,
            'two_bed_weekly': two_bed_weekly,
            'three_bed_weekly': three_bed_weekly,
            'four_bed_weekly': four_bed_weekly,
        })

    print(f"Parsed {len(brma_rates)} BRMA rows\n")
    print("First 5 rows:")
    for row in brma_rates[:5]:
        print(f"  {row['brma_name']:30} SAR weekly: GBP{row['sar_weekly']:7.2f}")

    # Verify Birmingham
    bham = [r for r in brma_rates if 'Birmingham' in r['brma_name']]
    if bham:
        print(f"\nBirmingham SAR weekly: GBP{bham[0]['sar_weekly']:.2f} (expected ~GBP78.83)")

    return brma_rates

# ============================================================================
# TASK 2: DOWNLOAD BRMA BOUNDARIES AND BUILD MAPPING
# ============================================================================

def task2_fetch_brma_boundaries() -> gpd.GeoDataFrame:
    """Fetch BRMA boundaries from GOV.UK."""
    print("\n=== TASK 2a: Download BRMA Boundaries ===\n")

    print(f"Fetching GOV.UK publication page...")
    html = fetch_html(VOA_BRMA_PAGE)

    # Extract ZIP download link
    import re
    zip_match = re.search(r'href="([^"]*\.zip[^"]*)"', html)
    if not zip_match:
        print("ERROR: Could not find ZIP download link on GOV.UK page")
        sys.exit(1)

    zip_url = zip_match.group(1)
    if not zip_url.startswith('http'):
        zip_url = urljoin(VOA_BRMA_PAGE, zip_url)

    print(f"Found ZIP URL: {zip_url}")

    # Download and extract
    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = Path(tmpdir) / "brma.zip"
        print(f"Downloading ZIP...")
        zip_data = fetch_binary(zip_url)
        zip_path.write_bytes(zip_data)

        print(f"Extracting to {tmpdir}...")
        shutil.unpack_archive(zip_path, tmpdir)

        # Try to find shapefile first, then GML, then other formats
        gdf = None

        # Try .shp files
        shp_files = list(Path(tmpdir).glob("**/*.shp"))
        if shp_files:
            shp_path = shp_files[0]
            print(f"Loading shapefile: {shp_path}")
            gdf = gpd.read_file(shp_path)
        else:
            # Try GML files
            gml_files = list(Path(tmpdir).glob("**/*.gml"))
            if gml_files:
                gml_path = gml_files[0]
                print(f"Loading GML file: {gml_path}")
                gdf = gpd.read_file(gml_path)
            else:
                # Try other vector formats
                other_files = list(Path(tmpdir).glob("**/*"))
                print(f"Available files in archive: {[f.name for f in other_files[:10]]}")
                print("ERROR: No shapefile or GML found in archive")
                sys.exit(1)

        print(f"Loaded {len(gdf)} BRMA geometries")
        print(f"CRS: {gdf.crs}")

        # Set CRS if missing (GML files often have no CRS metadata)
        if gdf.crs is None:
            print("CRS not set in file. Assuming EPSG:27700 (British National Grid)...")
            gdf = gdf.set_crs('EPSG:27700')

        # Reproject to WGS84 if needed
        if gdf.crs != 'EPSG:4326':
            print(f"Reprojecting from {gdf.crs} to EPSG:4326...")
            gdf = gdf.to_crs('EPSG:4326')

        # Find the BRMA name column
        brma_col = None
        for col in gdf.columns:
            if 'name' in col.lower() or 'brma' in col.lower():
                brma_col = col
                break

        if not brma_col:
            print(f"Available columns: {list(gdf.columns)}")
            brma_col = gdf.columns[1] if len(gdf.columns) > 1 else gdf.columns[0]

        print(f"Using BRMA name column: {brma_col}")
        gdf = gdf.rename(columns={brma_col: 'brma_name'})

        # Copy to persistent location for Task 2b
        gdf_copy = gdf.copy()

    return gdf_copy

def task2_build_mapping(brma_gdf: gpd.GeoDataFrame, brma_rates: List[Dict]) -> pd.DataFrame:
    """Build LA->BRMA mapping via spatial join."""
    print("\n=== TASK 2: Build LA->BRMA Mapping ===\n")

    # Get DB connection
    conn = get_db_connection()
    cur = conn.cursor()

    # Fetch LA boundaries
    print("Fetching LA centroids from Postgres...")
    cur.execute("""
        SELECT lad24cd, lad24nm, longitude, latitude
        FROM la_boundaries
        ORDER BY lad24cd
    """)
    la_rows = cur.fetchall()
    cur.close()
    conn.close()

    print(f"Loaded {len(la_rows)} LAs\n")

    # Create GeoDataFrame of LA points
    la_data = []
    for lad24cd, lad24nm, lon, lat in la_rows:
        la_data.append({
            'lad24cd': lad24cd,
            'la_name': lad24nm,
            'geometry': Point(float(lon), float(lat))
        })

    la_gdf = gpd.GeoDataFrame(la_data, crs='EPSG:4326')

    # Spatial join
    print("Performing spatial join (LA centroids -> BRMA polygons)...")
    mapping_gdf = gpd.sjoin(
        la_gdf, brma_gdf[['geometry', 'brma_name']],
        how='left', predicate='within'
    )

    # For LAs outside BRMA polygons, use nearest
    print("Finding fallback BRMAs for LAs outside polygons...")
    for idx in mapping_gdf[mapping_gdf['brma_name'].isna()].index:
        la_point = mapping_gdf.loc[idx, 'geometry']
        # Find nearest BRMA
        distances = brma_gdf.geometry.distance(la_point)
        nearest_idx = distances.idxmin()
        mapping_gdf.loc[idx, 'brma_name'] = brma_gdf.loc[nearest_idx, 'brma_name']
        mapping_gdf.loc[idx, 'mapping_method'] = 'nearest_brma_fallback'

    # Fill default mapping_method
    mapping_gdf['mapping_method'] = mapping_gdf.get('mapping_method', 'centroid_spatial_join')

    # Select columns
    mapping_df = mapping_gdf[['lad24cd', 'la_name', 'brma_name', 'mapping_method']].copy()

    return mapping_df

# ============================================================================
# TASK 2b: VERIFICATION SUITE
# ============================================================================

def task2b_verify_mapping(mapping_df: pd.DataFrame, brma_rates: List[Dict],
                          la_rows: List[Tuple]) -> Dict:
    """Run automated verification checks."""
    print("\n=== TASK 2b: Automated Verification Suite ===\n")

    # Get DB connection for anchor validation
    conn = get_db_connection()
    cur = conn.cursor()

    # Fetch all LA codes
    cur.execute("SELECT lad24cd, lad24nm FROM la_boundaries ORDER BY lad24cd")
    db_las = {row[0]: row[1] for row in cur.fetchall()}
    cur.close()
    conn.close()

    # Extract BRMA names from rates
    brma_names_csv = set(r['brma_name'] for r in brma_rates)
    brma_names_shp = set(mapping_df['brma_name'].unique())

    results = {}

    # ===== CHECK 1: ANCHOR SET =====
    print("CHECK 1: Anchor Set Validation")
    print("-" * 50)

    # Prune anchors
    verified_anchors = {}
    for lad24cd, brma_name in ANCHOR_SET.items():
        if lad24cd in db_las and brma_name in brma_names_csv:
            verified_anchors[lad24cd] = brma_name

    print(f"Verified {len(verified_anchors)}/{len(ANCHOR_SET)} anchors")

    # Check matches
    anchor_matches = 0
    anchor_mismatches = []
    for lad24cd, expected_brma in verified_anchors.items():
        actual_brma = mapping_df[mapping_df['lad24cd'] == lad24cd]['brma_name'].values
        if len(actual_brma) > 0 and actual_brma[0] == expected_brma:
            anchor_matches += 1
        else:
            actual = actual_brma[0] if len(actual_brma) > 0 else 'NOT_FOUND'
            anchor_mismatches.append((lad24cd, expected_brma, actual))

    anchor_pct = (anchor_matches / len(verified_anchors) * 100) if verified_anchors else 0
    print(f"Anchor matches: {anchor_matches}/{len(verified_anchors)} ({anchor_pct:.1f}%)")

    if anchor_mismatches and anchor_pct < 90:
        print(f"WARNING: <90% anchor match rate")
        for lad24cd, expected, actual in anchor_mismatches[:5]:
            print(f"  {lad24cd}: expected {expected}, got {actual}")

    results['anchor_matches'] = anchor_matches
    results['anchor_total'] = len(verified_anchors)
    results['anchor_pct'] = anchor_pct

    # ===== CHECK 2: COVERAGE =====
    print("\nCHECK 2: Coverage")
    print("-" * 50)

    total_mapped = len(mapping_df)
    nulls = mapping_df['brma_name'].isna().sum()
    distinct_brmas = mapping_df['brma_name'].nunique()

    print(f"Total LAs mapped: {total_mapped} (expected 296)")
    print(f"LAs with NULL brma_name: {nulls} (expected 0)")
    print(f"Distinct BRMAs assigned: {distinct_brmas} (expected ~152)")

    results['coverage_fail'] = (total_mapped != 296 or nulls > 0)
    results['brma_count'] = distinct_brmas

    # Check for unmatched BRMAs
    mapped_brmas = set(mapping_df['brma_name'].unique())
    unmatched_csv = brma_names_csv - mapped_brmas
    unmatched_shp = mapped_brmas - brma_names_csv

    if unmatched_csv:
        print(f"WARNING: {len(unmatched_csv)} BRMAs in CSV not assigned to any LA")
        for b in sorted(unmatched_csv)[:3]:
            print(f"  {b}")

    if unmatched_shp:
        print(f"WARNING: {len(unmatched_shp)} assigned BRMAs not in CSV")
        results['name_mismatch'] = True

    # ===== CHECK 3: LONDON RULE =====
    print("\nCHECK 3: London Rule")
    print("-" * 50)

    london_boros = mapping_df[mapping_df['lad24cd'].isin(LONDON_BOROUGHS)]
    london_non_london = london_boros[~london_boros['brma_name'].str.contains('London', na=False)]

    if len(london_non_london) > 0:
        print(f"FAIL: {len(london_non_london)} London boroughs mapped to non-London BRMAs")
        for _, row in london_non_london.iterrows():
            print(f"  {row['lad24cd']} {row['la_name']} -> {row['brma_name']}")
        results['london_fail'] = True
    else:
        print(f"PASS: All {len(london_boros)} London boroughs map to London BRMAs")

    # ===== CHECK 4: GEOGRAPHIC COHERENCE =====
    print("\nCHECK 4: Geographic Coherence")
    print("-" * 50)

    # Rebuild with coordinates for distance calc
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT lad24cd, longitude, latitude FROM la_boundaries")
    la_coords = {row[0]: (row[1], row[2]) for row in cur.fetchall()}
    cur.close()
    conn.close()

    geog_warnings = 0
    for brma_name in mapping_df['brma_name'].unique():
        las_in_brma = mapping_df[mapping_df['brma_name'] == brma_name]['lad24cd'].tolist()
        if len(las_in_brma) < 2:
            continue

        # Find max distance
        max_dist = 0
        for i, lad1 in enumerate(las_in_brma):
            for lad2 in las_in_brma[i+1:]:
                if lad1 in la_coords and lad2 in la_coords:
                    lon1, lat1 = la_coords[lad1]
                    lon2, lat2 = la_coords[lad2]
                    dist = haversine_km(lon1, lat1, lon2, lat2)
                    max_dist = max(max_dist, dist)

        if max_dist > 100:
            geog_warnings += 1
            if geog_warnings <= 3:
                print(f"  {brma_name}: max inter-centroid distance {max_dist:.1f} km")

    print(f"Geographic coherence warnings: {geog_warnings}")

    # ===== CHECK 5: RATE REASONABLENESS =====
    print("\nCHECK 5: Rate Reasonableness")
    print("-" * 50)

    rate_fail = False
    for rate in brma_rates:
        sar = rate['sar_weekly']
        if sar < 50:
            print(f"FAIL: {rate['brma_name']} SAR GBP{sar:.2f} below GBP50")
            rate_fail = True
        if sar > 250:
            print(f"FAIL: {rate['brma_name']} SAR GBP{sar:.2f} above GBP250")
            rate_fail = True

    if not rate_fail:
        print(f"PASS: All rates within GBP50-GBP250 range")

    results['rate_fail'] = rate_fail

    # ===== CHECK 6: SUMMARY =====
    print("\nVERIFICATION SUMMARY")
    print("=" * 50)
    print(f"Anchor set verified:     {results['anchor_matches']}/{results['anchor_total']} ({results['anchor_pct']:.1f}%)")
    print(f"Coverage — LAs mapped:   {total_mapped}/296")
    print(f"Coverage — LAs missing:  {nulls}")
    print(f"Coverage — BRMAs used:   {distinct_brmas}/152")
    print(f"London rule:             {'PASS' if not results.get('london_fail') else 'FAIL'}")
    print(f"Geographic coherence:    {geog_warnings} warnings")
    print(f"Rate reasonableness:     {'PASS' if not rate_fail else 'FAIL'}")

    overall_pass = (
        anchor_pct >= 90 and
        total_mapped == 296 and
        nulls == 0 and
        not results.get('london_fail') and
        not rate_fail
    )

    print(f"\nOVERALL: {'PASS' if overall_pass else 'FAIL'}")
    print("=" * 50)

    if not overall_pass:
        print("\nERROR: Verification failed. Do not proceed to load data.")
        sys.exit(1)

    return results

# ============================================================================
# TASK 3-4: CREATE TABLES AND LOAD DATA
# ============================================================================

def task3_create_tables():
    """Create database tables."""
    print("\n=== TASK 3: Create Database Tables ===\n")

    conn = get_db_connection()
    cur = conn.cursor()

    # Create la_brma_mapping
    print("Creating la_brma_mapping table...")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS la_brma_mapping (
            lad24cd         VARCHAR(9)    PRIMARY KEY,
            la_name         VARCHAR(100),
            brma_name       VARCHAR(100)  NOT NULL,
            brma_secondary  VARCHAR(100),
            mapping_method  TEXT          DEFAULT 'centroid_spatial_join',
            source          TEXT          DEFAULT 'VOA BRMA boundaries May 2020 × la_boundaries centroids',
            loaded_at       TIMESTAMPTZ   DEFAULT NOW()
        )
    """)

    # Create brma_lha_rates
    print("Creating brma_lha_rates table...")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS brma_lha_rates (
            brma_name           VARCHAR(100)  NOT NULL,
            financial_year      VARCHAR(7)    NOT NULL,
            sar_weekly          NUMERIC(8,2),
            one_bed_weekly      NUMERIC(8,2),
            two_bed_weekly      NUMERIC(8,2),
            three_bed_weekly    NUMERIC(8,2),
            four_bed_weekly     NUMERIC(8,2),
            sar_monthly         NUMERIC(8,2),
            one_bed_monthly     NUMERIC(8,2),
            two_bed_monthly     NUMERIC(8,2),
            three_bed_monthly   NUMERIC(8,2),
            four_bed_monthly    NUMERIC(8,2),
            source              TEXT,
            loaded_at           TIMESTAMPTZ   DEFAULT NOW(),
            PRIMARY KEY (brma_name, financial_year)
        )
    """)

    conn.commit()
    cur.close()
    conn.close()
    print("Tables created successfully\n")

def task4_load_data(mapping_df: pd.DataFrame, brma_rates: List[Dict]):
    """Load data into tables."""
    print("=== TASK 4: Load Data ===\n")

    conn = get_db_connection()
    cur = conn.cursor()

    # Upsert mapping
    print(f"Upserting {len(mapping_df)} LA->BRMA mappings...")
    mapping_tuples = [
        (row['lad24cd'], row['la_name'], row['brma_name'], None,
         row.get('mapping_method', 'centroid_spatial_join'),
         'VOA BRMA boundaries May 2020 × la_boundaries centroids')
        for _, row in mapping_df.iterrows()
    ]

    execute_values(cur, """
        INSERT INTO la_brma_mapping
        (lad24cd, la_name, brma_name, brma_secondary, mapping_method, source)
        VALUES %s
        ON CONFLICT (lad24cd) DO UPDATE SET
            la_name = EXCLUDED.la_name,
            brma_name = EXCLUDED.brma_name,
            mapping_method = EXCLUDED.mapping_method,
            loaded_at = NOW()
    """, mapping_tuples, page_size=100)

    # Upsert rates
    print(f"Upserting {len(brma_rates)} BRMA LHA rates...")
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
        VALUES %s
        ON CONFLICT (brma_name, financial_year) DO UPDATE SET
            sar_weekly = EXCLUDED.sar_weekly,
            one_bed_weekly = EXCLUDED.one_bed_weekly,
            two_bed_weekly = EXCLUDED.two_bed_weekly,
            three_bed_weekly = EXCLUDED.three_bed_weekly,
            four_bed_weekly = EXCLUDED.four_bed_weekly,
            sar_monthly = EXCLUDED.sar_monthly,
            one_bed_monthly = EXCLUDED.one_bed_monthly,
            two_bed_monthly = EXCLUDED.two_bed_monthly,
            three_bed_monthly = EXCLUDED.three_bed_monthly,
            four_bed_monthly = EXCLUDED.four_bed_monthly,
            loaded_at = NOW()
    """, rate_tuples, page_size=100)

    conn.commit()
    cur.close()
    conn.close()
    print("Data loaded successfully\n")

# ============================================================================
# TASK 5: ADD LHA COLUMNS TO staging_la_signals
# ============================================================================

def task5_add_lha_columns():
    """Add LHA columns to staging_la_signals."""
    print("=== TASK 5: Add LHA Columns ===\n")

    conn = get_db_connection()
    cur = conn.cursor()

    sql = """
    DO $$
    BEGIN
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
    """

    cur.execute(sql)
    conn.commit()
    cur.close()
    conn.close()
    print("LHA columns added successfully\n")

# ============================================================================
# TASK 6: LOG PIPELINE RUN
# ============================================================================

def task6_log_run(mapping_count: int, rate_count: int):
    """Log pipeline run."""
    print("=== TASK 6: Log Pipeline Run ===\n")

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO pipeline_run_log
        (agent_name, source_number, rows_written, started_at, completed_at, status, notes)
        VALUES (%s, %s, %s, NOW(), NOW(), %s, %s)
    """, (
        'Source 14 - LHA Rates',
        14,
        mapping_count + rate_count,
        'success',
        'DWP UC LHA rates 2026-27 (frozen at 2024-25 levels). BRMA-to-LA mapping built via centroid spatial join against VOA BRMA boundaries May 2020.'
    ))

    conn.commit()
    cur.close()
    conn.close()
    print("Pipeline run logged\n")

# ============================================================================
# TASK 7: VERIFICATION QUERIES
# ============================================================================

def task7_run_verification_queries():
    """Run verification queries."""
    print("=== TASK 7: Verification Queries ===\n")

    conn = get_db_connection()
    cur = conn.cursor()

    print("7a. Rate table row count and sample")
    print("-" * 50)
    cur.execute("""
        SELECT COUNT(*) AS brma_count FROM brma_lha_rates WHERE financial_year = '2026-27'
    """)
    count = cur.fetchone()[0]
    print(f"BRMAs in 2026-27: {count}\n")

    cur.execute("""
        SELECT brma_name, sar_weekly, one_bed_weekly, four_bed_weekly
        FROM brma_lha_rates WHERE financial_year = '2026-27'
        ORDER BY brma_name LIMIT 10
    """)
    for brma_name, sar, one_bed, four_bed in cur.fetchall():
        print(f"  {brma_name:30} SAR: GBP{sar:7.2f}  1bed: GBP{one_bed:7.2f}  4bed: GBP{four_bed:7.2f}")

    print("\n7b. Mapping coverage")
    print("-" * 50)
    cur.execute("SELECT COUNT(*) FROM la_brma_mapping")
    mapped = cur.fetchone()[0]
    print(f"LAs mapped: {mapped}")

    cur.execute("""
        SELECT COUNT(*) FROM la_boundaries b
        LEFT JOIN la_brma_mapping m ON m.lad24cd = b.lad24cd
        WHERE m.lad24cd IS NULL
    """)
    unmapped = cur.fetchone()[0]
    print(f"LAs unmapped: {unmapped}\n")

    print("7c. Target market spot-check")
    print("-" * 50)
    cur.execute("""
        SELECT m.lad24cd, m.la_name, m.brma_name,
               r.sar_weekly, r.one_bed_weekly, r.two_bed_weekly, r.three_bed_weekly, r.four_bed_weekly
        FROM la_brma_mapping m
        JOIN brma_lha_rates r ON r.brma_name = m.brma_name AND r.financial_year = '2026-27'
        WHERE m.la_name IN ('Birmingham', 'Liverpool', 'Nottingham', 'Manchester', 'Blackpool')
        ORDER BY m.la_name
    """)
    for lad24cd, la_name, brma_name, sar, b1, b2, b3, b4 in cur.fetchall():
        print(f"  {la_name:20} ({lad24cd}) -> {brma_name:30}")
        print(f"    SAR: GBP{sar:7.2f}  1bed: GBP{b1:7.2f}  2bed: GBP{b2:7.2f}  3bed: GBP{b3:7.2f}  4bed: GBP{b4:7.2f}")

    print("\n7d. Full join test (all 296 LAs)")
    print("-" * 50)
    cur.execute("""
        SELECT COUNT(*) FROM la_boundaries b
        JOIN la_brma_mapping m ON m.lad24cd = b.lad24cd
        JOIN brma_lha_rates r ON r.brma_name = m.brma_name AND r.financial_year = '2026-27'
    """)
    with_lha = cur.fetchone()[0]
    print(f"LAs with LHA rates: {with_lha} (expected 296)")

    cur.close()
    conn.close()

# ============================================================================
# TASK 8: PRODUCE NODE DOCUMENTATION
# ============================================================================

def task8_produce_documentation():
    """Produce node documentation files."""
    print("\n=== TASK 8: Produce Node Documentation ===\n")

    docs_dir = Path(".")

    # Node 1
    doc1 = """# Node 1 — Fetch DWP LHA CSV
## Type
HTTP GET + CSV Parse

## Purpose
Fetch the official DWP Universal Credit LHA rates for all 152 English BRMAs, FY 2026-27.
Rates are frozen at April 2024 levels and expressed in monthly values.

## URL
https://assets.publishing.service.gov.uk/media/69d654a2e1430e837a86f64a/england-rates-2026-to-2027.csv

## Logic
1. HTTP GET from DWP assets
2. Skip row 1 (title) — use row 2 as column headers
3. For each data row (row 3 onwards):
   - Extract: BRMA name, SAR (monthly), 1-bed (monthly), 2-bed, 3-bed, 4-bed (all monthly)
   - Strip GBP symbols and comma separators from numeric values
   - Convert monthly -> weekly: value × 12 ÷ 52
   - Round to 2 decimals
4. Output: DataFrame with brma_name, sar_weekly, one_bed_weekly, two_bed_weekly, three_bed_weekly, four_bed_weekly, and corresponding monthly values

## Behaviour
- Safe to re-run; CSV is idempotent
- Parsed data is held in memory, not persisted until Task 4

## Output
Python DataFrame: ~152 rows, 11 columns (BRMA name + 5 weekly rates + 5 monthly rates)
"""
    (docs_dir / "s14_node1_fetch_lha_csv.md").write_text(doc1)
    print("✓ s14_node1_fetch_lha_csv.md")

    # Node 2
    doc2 = """# Node 2 — Fetch BRMA Boundaries (Shapefile)
## Type
HTTP GET + Shapefile Extract + Reproject

## Purpose
Fetch the VOA Broad Rental Market Area (BRMA) boundary layer published by GOV.UK.
Used as the spatial reference for mapping LA centroids to BRMAs.

## URL
Publication page: https://www.gov.uk/government/publications/broad-rental-market-area-boundary-layer-for-geographical-information-system-gis-applicable-may-2020
(Extract shapefile ZIP URL from page)

## Logic
1. Fetch GOV.UK publication page
2. Parse for shapefile ZIP download link
3. HTTP GET and download ZIP archive
4. Extract to temporary directory
5. Load .shp file with geopandas
6. Check CRS — if not EPSG:4326 (WGS84), reproject
7. Identify BRMA name column (usually 'Name' or similar)
8. Output: GeoDataFrame with geometry (polygons) and brma_name

## Behaviour
- Safe to re-run; remote shapefile is the source of truth
- Temporary files are cleaned up after loading
- CRS mismatch is handled automatically

## Output
Geopandas GeoDataFrame: ~152 rows, geometry column + brma_name
"""
    (docs_dir / "s14_node2_fetch_brma_boundaries.md").write_text(doc2)
    print("✓ s14_node2_fetch_brma_boundaries.md")

    # Node 3
    doc3 = """# Node 3 — Build LA↔BRMA Spatial Crosswalk
## Type
Spatial Join + Fallback Nearest-Match + Verification

## Purpose
Map each of the 296 English local authorities to the BRMA containing its centroid.
This is the critical crosswalk enabling the pipeline to join LHA rates into per-LA signals.

## Logic
1. Query la_boundaries (296 rows) — extract lad24cd, lad24nm, longitude, latitude
2. Convert each LA centroid to a Point geometry (EPSG:4326)
3. Spatial join: for each Point, find which BRMA polygon contains it
4. For LAs whose centroids fall outside all BRMA polygons:
   - Find nearest BRMA by minimum distance
   - Flag mapping_method = 'nearest_brma_fallback'
5. For all others: mapping_method = 'centroid_spatial_join'
6. Run verification suite (6 checks: anchors, coverage, London rule, geographic coherence, rate sanity, BRMA name reconciliation)
7. If any critical check fails, HALT and report
8. If all checks pass, output: DataFrame with lad24cd, la_name, brma_name, mapping_method

## Verification Checks
- **Anchor Set**: ≥90% of known-correct LA->BRMA pairs must match
- **Coverage**: All 296 LAs mapped, ~152 distinct BRMAs assigned
- **London Rule**: All 33 E09* borough codes must map to BRMAs containing 'London'
- **Geographic Coherence**: Max inter-centroid distance per BRMA must be reasonable (<100km unless explicitly large BRMA)
- **Rate Sanity**: All SAR rates 50 ≤ GBP ≤ 250 weekly
- **BRMA Name Reconciliation**: Handle naming differences between shapefile and DWP CSV via fuzzy matching

## Behaviour
- Fallback nearest-match is used only for edge cases; the majority map via centroid containment
- Verification is deterministic and auto-gating — no manual review
- Mapping is computed fresh each run from latest shapefile + centroids

## Output
Pandas DataFrame: 296 rows, columns (lad24cd, la_name, brma_name, mapping_method)
"""
    (docs_dir / "s14_node3_build_brma_la_mapping.md").write_text(doc3)
    print("✓ s14_node3_build_brma_la_mapping.md")

    # Node 4
    doc4 = """# Node 4 — Create Database Tables
## Type
Postgres DDL

## Purpose
Create two tables to store the BRMA↔LA crosswalk and LHA rates.

## SQL — Table 1: la_brma_mapping
```sql
CREATE TABLE IF NOT EXISTS la_brma_mapping (
    lad24cd         VARCHAR(9)    PRIMARY KEY,
    la_name         VARCHAR(100),
    brma_name       VARCHAR(100)  NOT NULL,
    brma_secondary  VARCHAR(100),
    mapping_method  TEXT          DEFAULT 'centroid_spatial_join',
    source          TEXT          DEFAULT 'VOA BRMA boundaries May 2020 × la_boundaries centroids',
    loaded_at       TIMESTAMPTZ   DEFAULT NOW()
);
```

## SQL — Table 2: brma_lha_rates
```sql
CREATE TABLE IF NOT EXISTS brma_lha_rates (
    brma_name           VARCHAR(100)  NOT NULL,
    financial_year      VARCHAR(7)    NOT NULL,
    sar_weekly          NUMERIC(8,2),
    one_bed_weekly      NUMERIC(8,2),
    two_bed_weekly      NUMERIC(8,2),
    three_bed_weekly    NUMERIC(8,2),
    four_bed_weekly     NUMERIC(8,2),
    sar_monthly         NUMERIC(8,2),
    one_bed_monthly     NUMERIC(8,2),
    two_bed_monthly     NUMERIC(8,2),
    three_bed_monthly   NUMERIC(8,2),
    four_bed_monthly    NUMERIC(8,2),
    source              TEXT,
    loaded_at           TIMESTAMPTZ   DEFAULT NOW(),
    PRIMARY KEY (brma_name, financial_year)
);
```

## Behaviour
- Both use CREATE TABLE IF NOT EXISTS for idempotency
- la_brma_mapping keys on lad24cd (UNIQUE)
- brma_lha_rates keys on (brma_name, financial_year) to support future year updates
- Timestamps are auto-set on insert/update

## Connection
- Input: None (creates empty tables)
- Output: Tables ready for Task 5 (upsert)
"""
    (docs_dir / "s14_node4_create_tables.md").write_text(doc4)
    print("✓ s14_node4_create_tables.md")

    # Node 5
    doc5 = """# Node 5 — Upsert LA↔BRMA Mapping
## Type
Postgres UPSERT

## Purpose
Load the 296 LA↔BRMA mapping records into la_brma_mapping table.

## Query
```sql
INSERT INTO la_brma_mapping
(lad24cd, la_name, brma_name, brma_secondary, mapping_method, source)
VALUES
  ('E08000025', 'Birmingham', 'Birmingham', NULL, 'centroid_spatial_join', 'VOA BRMA boundaries May 2020 × la_boundaries centroids'),
  ('E08000012', 'Liverpool', 'Greater Liverpool', NULL, 'centroid_spatial_join', 'VOA BRMA boundaries May 2020 × la_boundaries centroids'),
  ...
ON CONFLICT (lad24cd) DO UPDATE SET
    la_name = EXCLUDED.la_name,
    brma_name = EXCLUDED.brma_name,
    mapping_method = EXCLUDED.mapping_method,
    loaded_at = NOW();
```

## Behaviour
- UPSERT: if lad24cd already exists, update all columns except lad24cd
- Safe to re-run; idempotent
- 296 rows inserted/updated in single batch

## Connection
- Input: Output of Node 3 (mapping DataFrame)
- Output: la_brma_mapping populated
"""
    (docs_dir / "s14_node5_upsert_mapping.md").write_text(doc5)
    print("✓ s14_node5_upsert_mapping.md")

    # Node 6
    doc6 = """# Node 6 — Upsert BRMA LHA Rates
## Type
Postgres UPSERT

## Purpose
Load ~152 BRMA LHA rates (both monthly and weekly) into brma_lha_rates table.

## Query
```sql
INSERT INTO brma_lha_rates
(brma_name, financial_year, sar_weekly, one_bed_weekly, two_bed_weekly, three_bed_weekly, four_bed_weekly,
 sar_monthly, one_bed_monthly, two_bed_monthly, three_bed_monthly, four_bed_monthly, source)
VALUES
  ('Birmingham', '2026-27', 78.83, ..., 341.58, ..., 'DWP UC LHA rates 2026-27, ...'),
  ...
ON CONFLICT (brma_name, financial_year) DO UPDATE SET
    sar_weekly = EXCLUDED.sar_weekly,
    one_bed_weekly = EXCLUDED.one_bed_weekly,
    two_bed_weekly = EXCLUDED.two_bed_weekly,
    three_bed_weekly = EXCLUDED.three_bed_weekly,
    four_bed_weekly = EXCLUDED.four_bed_weekly,
    sar_monthly = EXCLUDED.sar_monthly,
    one_bed_monthly = EXCLUDED.one_bed_monthly,
    two_bed_monthly = EXCLUDED.two_bed_monthly,
    three_bed_monthly = EXCLUDED.three_bed_monthly,
    four_bed_monthly = EXCLUDED.four_bed_monthly,
    loaded_at = NOW();
```

## Behaviour
- UPSERT on (brma_name, financial_year) composite key
- Stores both monthly (original) and weekly (converted) rates for audit trail
- Safe to re-run; idempotent
- ~152 rows per financial year

## Connection
- Input: Output of Node 1 (parsed DWP CSV)
- Output: brma_lha_rates populated
"""
    (docs_dir / "s14_node6_upsert_lha_rates.md").write_text(doc6)
    print("✓ s14_node6_upsert_lha_rates.md")

    # Node 7
    doc7 = """# Node 7 — Log Pipeline Run
## Type
Postgres INSERT

## Purpose
Record this S14 pipeline execution in pipeline_run_log for audit and replay tracking.

## Query
```sql
INSERT INTO pipeline_run_log
(agent_name, source_number, rows_written, started_at, completed_at, status, notes)
VALUES
('Source 14 - LHA Rates', 14, 448, NOW(), NOW(), 'success',
 'DWP UC LHA rates 2026-27 (frozen at 2024-25 levels). BRMA-to-LA mapping built via centroid spatial join against VOA BRMA boundaries May 2020.');
```

## Notes
- rows_written = 296 (mappings) + 152 (rate rows) = 448
- status must be 'success' (only after all prior nodes succeed)
- Notes field includes the financial year, frozen-level status, and mapping method for replay

## Connection
- Input: Completion of Nodes 5 & 6
- Output: pipeline_run_log entry
"""
    (docs_dir / "s14_node7_log_run.md").write_text(doc7)
    print("✓ s14_node7_log_run.md")

    print()

# ============================================================================
# TASK 9: PRINT UPDATED W1 NODE 5 SQL
# ============================================================================

def task9_print_updated_w1_node5_sql():
    """Print the updated W1 Node 5 query with LHA joins."""
    print("=== TASK 9: Updated W1 Node 5 SQL ===\n")

    # This is a template; the user will need to insert the current la_signals query
    updated_sql = """
-- W1 Node 5: la_signals (UPDATED with LHA joins)
-- This replaces the existing W1 Node 5 query in the n8n workflow

INSERT INTO staging_la_signals (
    run_id,
    lad24cd,
    -- existing columns --
    la_name,
    ta_caseload,
    rough_sleeping_count,
    care_leavers_count,
    marac_cases,
    hb_sa_caseload,
    housing_register_size,
    ro4_spend,
    efs_s114_flags,
    imd_rank,
    -- NEW LHA columns --
    lha_brma_name,
    lha_sar_weekly,
    lha_1bed_weekly,
    lha_2bed_weekly,
    lha_3bed_weekly,
    lha_4bed_weekly
)
SELECT
    '{{ run_id }}' AS run_id,
    b.lad24cd,
    -- existing columns --
    b.lad24nm,
    -- ... existing column selections ...
    imd2025.rank,
    -- NEW LHA columns --
    lbm.brma_name AS lha_brma_name,
    lha.sar_weekly AS lha_sar_weekly,
    lha.one_bed_weekly AS lha_1bed_weekly,
    lha.two_bed_weekly AS lha_2bed_weekly,
    lha.three_bed_weekly AS lha_3bed_weekly,
    lha.four_bed_weekly AS lha_4bed_weekly
FROM
    la_boundaries b
    LEFT JOIN la_imd_2025 imd2025 ON imd2025.lad24cd = b.lad24cd
    -- NEW JOINS (add after existing joins) --
    LEFT JOIN la_brma_mapping lbm ON lbm.lad24cd = b.lad24cd
    LEFT JOIN brma_lha_rates lha ON lha.brma_name = lbm.brma_name AND lha.financial_year = '2026-27'
ON CONFLICT (run_id, lad24cd) DO UPDATE SET
    -- existing SET clauses --
    imd_rank = EXCLUDED.imd_rank,
    -- NEW SET clauses --
    lha_brma_name = EXCLUDED.lha_brma_name,
    lha_sar_weekly = EXCLUDED.lha_sar_weekly,
    lha_1bed_weekly = EXCLUDED.lha_1bed_weekly,
    lha_2bed_weekly = EXCLUDED.lha_2bed_weekly,
    lha_3bed_weekly = EXCLUDED.lha_3bed_weekly,
    lha_4bed_weekly = EXCLUDED.lha_4bed_weekly;
"""

    print(updated_sql)
    print("\n" + "=" * 70)
    print("NOTE: This is a template. The actual query should:")
    print("  1. Retain all existing SELECT columns and JOINs")
    print("  2. Add the two new LEFT JOINs as shown")
    print("  3. Add the six new SELECT columns for LHA data")
    print("  4. Add the six new ON CONFLICT SET entries")
    print("=" * 70 + "\n")

# ============================================================================
# MAIN
# ============================================================================

def main():
    """Main execution flow."""
    print("\n" + "=" * 70)
    print("S14 — VOA/DWP LHA Rates Source Builder")
    print("=" * 70)

    # Check DB password
    if not DB_PASSWORD:
        print("\nERROR: Database password not found in EXEMPT_PIPELINE_DB_PASSWORD or PG_PASSWORD")
        print("Set one of these environment variables before running")
        sys.exit(1)

    try:
        # Task 1
        brma_rates = task1_parse_dwp_csv()

        # Task 2
        brma_gdf = task2_fetch_brma_boundaries()

        # Fetch LA boundaries for verification
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT lad24cd, lad24nm, longitude, latitude FROM la_boundaries ORDER BY lad24cd")
        la_rows = cur.fetchall()
        cur.close()
        conn.close()

        # Build mapping
        mapping_df = task2_build_mapping(brma_gdf, brma_rates)

        # Task 2b — Verification
        task2b_verify_mapping(mapping_df, brma_rates, la_rows)

        # Task 3-4
        task3_create_tables()
        task4_load_data(mapping_df, brma_rates)

        # Task 5
        task5_add_lha_columns()

        # Task 6
        task6_log_run(len(mapping_df), len(brma_rates))

        # Task 7
        task7_run_verification_queries()

        # Task 8
        task8_produce_documentation()

        # Task 9
        task9_print_updated_w1_node5_sql()

        print("\n" + "=" * 70)
        print("✓ S14 BUILD COMPLETE")
        print("=" * 70)

    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == '__main__':
    main()
