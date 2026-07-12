# Node 1 - Fetch CQC care directory with filters

## Type
Python fetch + ODS-to-CSV conversion (`scripts/s11_cqc_fetch.py`)

## Purpose
Acquire the current edition of the CQC "Care directory with filters" file, the complete register of active CQC-regulated locations in England, and convert it to CSV for the downstream nodes. The download URL is extracted from the CQC data page on every run, never hardcoded: CQC is migrating its directory to a new digital system and the file URL moves with each edition.

## URL
Page (stable): `https://www.cqc.org.uk/about-us/transparency/using-cqc-data`
File link pattern extracted from the page: `href` ending `HSCA_Active_Locations*.ods` or `.xlsx`
July 2026 resolved URL: `https://www.cqc.org.uk/sites/default/files/2026-07/01_July_2026_HSCA_Active_Locations.ods`

## Logic
1. GET the using-cqc-data page and regex-extract the first link whose filename contains `HSCA_Active_Locations` (ods or xlsx accepted, both formats have been published historically). If no link matches, stop with an error rather than guessing an alternative URL.
2. Parse the file date from the filename (`01_July_2026_...` gives 2026-07-01) and stop if it cannot be parsed.
3. Stream-download to `data/raw/` (gitignored). Fail if under 5 MB.
4. Convert to CSV, one file per sheet, into `data/raw/s11_csv/`. The ODS route cannot use odfpy: content.xml is roughly 440 MB uncompressed and odfpy loads it whole (MemoryError). The script stream-parses with `xml.etree.iterparse` over the zip entry, honouring `number-columns-repeated` and `number-rows-repeated`, clearing elements as it goes. The xlsx route uses pandas/openpyxl.
5. Write the file date to `data/raw/s11_csv/FILE_DATE.txt` for the load node.

## Behaviour
Re-run safe: overwrites the raw file and CSVs in place. No database writes. Stops loudly if the page layout changes or the file moves, which the CQC migration makes likely at some point.

## Connection
- Input: none (entry node)
- Output: Node 2 - Process and filter

## Verified Output (2026-07-12)
File date 2026-07-01, 23,917,288 bytes. Sheets: README (21 rows), HSCA_Active_Locations (56,871 rows including header), Dual_Registration_Locations (855 rows). Header confirmed on row 1 of the data sheet; the metadata prose lives in the README sheet, not above the header.
