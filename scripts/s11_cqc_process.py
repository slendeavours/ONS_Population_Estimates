"""
s11_cqc_process.py — S11 Node 2: Filter to scope and normalise flags.

Purpose : From the converted HSCA_Active_Locations CSV, keep Adult social care
          directorate rows (the confirmed Phase 2 scope - 30,497 on the July
          2026 file), normalise Y/blank flag columns to booleans, parse dates
          and numerics, and write the processed CSV ready for LA mapping.
          Dormant and dual-registered rows are kept and flagged, not dropped:
          the dual Primary ID is stored so provision counts can dedupe per
          CQC's own guidance in the file README.
Inputs  : data/raw/s11_csv/HSCA_Active_Locations.csv
          data/raw/s11_csv/FILE_DATE.txt
Outputs : data/processed/cqc_locations_processed.csv
          Prints scope counts and flag totals.
"""
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "raw" / "s11_csv" / "HSCA_Active_Locations.csv"
OUT = ROOT / "data" / "processed" / "cqc_locations_processed.csv"

# source column -> output column for Y/blank flags
FLAG_COLS = {
    "Service type - Supported living service": "supported_living",
    "Regulated activity - Personal care": "personal_care",
    "Care home?": "care_home",
    "Service type - Domiciliary care service": "domiciliary_care",
    "Service type - Extra Care housing services": "extra_care_housing",
    "Service type - Shared Lives": "shared_lives",
    "Regulated activity - Accommodation for persons who require nursing or "
    "personal care": "accommodation_nursing_personal_care",
    "Service user band - Learning disabilities or autistic spectrum disorder":
        "band_learning_disabilities_autism",
    "Service user band - Mental Health": "band_mental_health",
    "Service user band - Younger Adults": "band_younger_adults",
    "Service user band - Older People": "band_older_people",
    "Service user band - Dementia": "band_dementia",
    "Service user band - People who misuse drugs and alcohol":
        "band_substance_misuse",
    "Service user band - Physical Disability": "band_physical_disability",
    "Service user band - People detained under the Mental Health Act":
        "band_detained_mha",
    "Dormant (Y/N)": "dormant",
}

TEXT_COLS = {
    "Location ID": "location_id",
    "Provider ID": "provider_id",
    "Provider Name": "provider_name",
    "Brand Name": "brand_name",
    "Location Name": "location_name",
    "Location Postal Code": "postcode",
    "Location Region": "region",
    "Location Local Authority": "la_name_cqc",
    "Location Latest Overall Rating": "latest_overall_rating",
    "Location Inspection Directorate": "inspection_directorate",
    "Location Primary Inspection Category": "primary_inspection_category",
    "Primary ID (Dual registration locations)": "dual_primary_id",
}


def main():
    df = pd.read_csv(SRC, dtype=str, keep_default_na=False)
    total = len(df)
    scope = df[df["Location Inspection Directorate"].str.strip()
               == "Adult social care"].copy()
    print(f"rows_total={total}")
    print(f"rows_in_scope={len(scope)}")

    out = pd.DataFrame()
    for src, dst in TEXT_COLS.items():
        out[dst] = scope[src].str.strip().replace("", None)
    # Brand Name uses '-' as its blank marker
    out["brand_name"] = out["brand_name"].replace("-", None)

    for src, dst in FLAG_COLS.items():
        out[dst] = scope[src].str.strip().str.upper().eq("Y")

    out["dual_registered"] = (scope["Location Dual Registered"].str.strip()
                              == "Dual Registration")
    # Inherited rating is Y/N/blank - keep blank as null, not false
    inh = scope["Inherited Rating (Y/N)"].str.strip().str.upper()
    out["inherited_rating"] = inh.map({"Y": True, "N": False})

    out["latitude"] = pd.to_numeric(scope["Location Latitude"], errors="coerce")
    out["longitude"] = pd.to_numeric(scope["Location Longitude"],
                                     errors="coerce")
    out["care_homes_beds"] = pd.to_numeric(scope["Care homes beds"],
                                           errors="coerce").astype("Int64")
    out["location_hsca_start_date"] = pd.to_datetime(
        scope["Location HSCA start date"], errors="coerce").dt.date
    out["rating_publication_date"] = pd.to_datetime(
        scope["Publication Date"], errors="coerce").dt.date

    assert out["location_id"].notna().all(), "blank location_id in scope"
    assert out["location_id"].is_unique, "duplicate location_id in scope"

    OUT.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(OUT, index=False)
    print(f"written={OUT}")
    for flag in ("supported_living", "personal_care", "care_home",
                 "domiciliary_care", "extra_care_housing", "shared_lives",
                 "dormant", "dual_registered"):
        print(f"{flag}=Y: {int(out[flag].sum())}")
    print(f"no_coordinates={int(out['latitude'].isna().sum())}")
    print("OK")


if __name__ == "__main__":
    main()
