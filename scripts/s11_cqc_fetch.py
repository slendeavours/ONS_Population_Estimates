"""
s11_cqc_fetch.py — S11 Node 1: Acquire the current CQC Care directory with filters.

Purpose : Fetch the CQC using-cqc-data page, extract the current
          "Care directory with filters" download URL (never hardcoded — CQC is
          mid-migration to a new digital system and the URL moves each edition),
          download the ODS, and stream-convert its sheets to CSV. The ODS
          content.xml is ~440 MB uncompressed, far beyond what odfpy can load,
          so conversion uses xml.etree.iterparse over the zip stream.
Inputs  : CQC data page (stable URL, below). No local inputs.
Outputs : data/raw/<original-name>.ods
          data/raw/s11_csv/<sheet>.csv  (one per sheet)
          Prints resolved URL, file date, sheet row counts.
"""
import csv
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

DATA_PAGE = "https://www.cqc.org.uk/about-us/transparency/using-cqc-data"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
           "exempt-pipeline-s11/1.0"}
ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
CSV_DIR = RAW_DIR / "s11_csv"

TABLE = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
OFFICE = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"
TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"


def find_download_url():
    page = requests.get(DATA_PAGE, headers=HEADERS, timeout=60)
    page.raise_for_status()
    # The filters file always carries HSCA_Active_Locations in its name.
    # Accept ods or xlsx — CQC has published both formats historically.
    pattern = re.compile(
        r'href="(https?://[^"]*HSCA_Active_Locations[^"]*\.(?:ods|xlsx))"', re.I)
    matches = pattern.findall(page.text)
    if not matches:
        sys.exit("ERROR: no HSCA_Active_Locations link found on the CQC data "
                 "page - the directory migration may have moved it. Stopping.")
    return matches[0]


def cell_value(cell):
    vt = cell.get(OFFICE + "value-type")
    if vt in ("float", "currency", "percentage"):
        return cell.get(OFFICE + "value")
    if vt == "boolean":
        return cell.get(OFFICE + "boolean-value")
    if vt == "date":
        return cell.get(OFFICE + "date-value")
    parts = ["".join(p.itertext()) for p in cell.iter(TEXT + "p")]
    return "\n".join(parts) if parts else ""


def ods_to_csv(ods_path, out_dir):
    """Stream one CSV per sheet without building a DOM."""
    out_dir.mkdir(parents=True, exist_ok=True)
    z = zipfile.ZipFile(ods_path)
    with z.open("content.xml") as f:
        writer = fh = sheet_name = None
        rows_out = 0
        for event, elem in ET.iterparse(f, events=("start", "end")):
            if event == "start" and elem.tag == TABLE + "table":
                sheet_name = elem.get(TABLE + "name") or "sheet"
                safe = "".join(c if c.isalnum() or c in "-_ " else "_"
                               for c in sheet_name).strip()
                fh = open(out_dir / f"{safe}.csv", "w", newline="",
                          encoding="utf-8")
                writer = csv.writer(fh)
                rows_out = 0
            elif event == "end" and elem.tag == TABLE + "table-row" and writer:
                row = []
                for cell in elem:
                    if cell.tag not in (TABLE + "table-cell",
                                        TABLE + "covered-table-cell"):
                        continue
                    rep = int(cell.get(TABLE + "number-columns-repeated", "1"))
                    val = cell_value(cell)
                    if rep > 1000 and val == "":
                        rep = 1  # trailing filler columns
                    row.extend([val] * rep)
                while row and row[-1] == "":
                    row.pop()
                if row:
                    rrep = int(elem.get(TABLE + "number-rows-repeated", "1"))
                    for _ in range(min(rrep, 1000)):
                        writer.writerow(row)
                        rows_out += 1
                elem.clear()
            elif event == "end" and elem.tag == TABLE + "table":
                fh.close()
                print(f"sheet={sheet_name} rows={rows_out}")
                writer = fh = None
                elem.clear()


def main():
    url = find_download_url()
    filename = url.rsplit("/", 1)[-1]
    print(f"url={url}")
    print(f"filename={filename}")

    # File date from the name, e.g. 01_July_2026_HSCA_Active_Locations.ods
    m = re.match(r"(\d{2})_([A-Za-z]+)_(\d{4})_", filename)
    if not m:
        sys.exit("ERROR: cannot parse file date from filename - stopping.")
    months = {m_: i for i, m_ in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"], 1)}
    file_date = f"{m.group(3)}-{months[m.group(2)]:02d}-{int(m.group(1)):02d}"
    print(f"file_date={file_date}")

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    dest = RAW_DIR / filename
    with requests.get(url, headers=HEADERS, timeout=600, stream=True) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    size = dest.stat().st_size
    print(f"downloaded={dest}")
    print(f"size_bytes={size}")
    if size < 5 * 1024 * 1024:
        sys.exit("ERROR: file under 5 MB - download suspect, stopping.")

    if filename.lower().endswith(".ods"):
        ods_to_csv(dest, CSV_DIR)
    else:
        # xlsx route (CQC has used it before): convert with pandas/openpyxl
        import pandas as pd
        CSV_DIR.mkdir(parents=True, exist_ok=True)
        xl = pd.ExcelFile(dest, engine="openpyxl")
        for s in xl.sheet_names:
            df = pd.read_excel(dest, sheet_name=s, engine="openpyxl", dtype=str)
            safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in s)
            df.to_csv(CSV_DIR / f"{safe}.csv", index=False)
            print(f"sheet={s} rows={len(df)}")

    # Stamp the file date where the downstream nodes can read it
    (CSV_DIR / "FILE_DATE.txt").write_text(file_date)
    print("OK")


if __name__ == "__main__":
    main()
