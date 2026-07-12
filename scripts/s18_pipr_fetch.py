"""
s18_pipr_fetch.py — S18 Phase 1: Acquire the latest ONS PIPR edition workbook.

Purpose : Fetch the PIPR dataset landing page, extract the most recent
          edition's xlsx link (never hardcoded — edition URLs change monthly),
          download it to data/raw/, and verify the download.
Inputs  : ONS landing page (stable URL, below). No local inputs.
Outputs : data/raw/pipr_<edition-date>.xlsx
          Prints edition slug, resolved URL, file size.
"""
import re
import sys
from pathlib import Path

import requests

LANDING = ("https://www.ons.gov.uk/economy/inflationandpriceindices/datasets/"
           "priceindexofprivaterentsukmonthlypricestatistics")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
           "exempt-pipeline-s18/1.0"}
RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"


def main():
    page = requests.get(LANDING, headers=HEADERS, timeout=60)
    page.raise_for_status()

    # hrefs look like: /file?uri=/economy/inflationandpriceindices/datasets/
    #   priceindexofprivaterentsukmonthlypricestatistics/<edition-slug>/<file>.xlsx
    pattern = re.compile(
        r'href="(/file\?uri=/economy/inflationandpriceindices/datasets/'
        r'priceindexofprivaterentsukmonthlypricestatistics/([^/"]+)/([^"]+?\.xlsx))"'
    )
    matches = pattern.findall(page.text)
    if not matches:
        print("ERROR: no xlsx edition link found on landing page", file=sys.stderr)
        sys.exit(1)

    # First match = most recent edition (ONS lists newest first)
    href, edition_slug, filename = matches[0]
    url = "https://www.ons.gov.uk" + href
    print(f"edition_slug={edition_slug}")
    print(f"filename={filename}")
    print(f"url={url}")

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    dest = RAW_DIR / f"pipr_{edition_slug}.xlsx"

    with requests.get(url, headers=HEADERS, timeout=300, stream=True) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)

    size = dest.stat().st_size
    print(f"downloaded={dest}")
    print(f"size_bytes={size}")
    if size <= 10 * 1024 * 1024:
        print("ERROR: file smaller than 10 MB — download suspect", file=sys.stderr)
        sys.exit(1)

    # Verify openable as a workbook
    import openpyxl
    wb = openpyxl.load_workbook(dest, read_only=True)
    print(f"sheets={len(wb.sheetnames)}")
    wb.close()
    print("OK")


if __name__ == "__main__":
    main()
