# Node 1 — Discover Source Files

## Type

Code (Python). `scripts/s22_ctb_discover.py`, functions `discover_council_taxbase` and `discover_table_615`.

## Purpose

Resolve both MHCLG source files from their publisher landing pages at run time and download them. No file URL is stored anywhere in the build. Halts if either file cannot be found.

## Credential

None. Both endpoints are public.

## Query / Code / URL (full content)

```python
API = "https://www.gov.uk/api/content"
COLLECTION_PATH = "/government/collections/council-taxbase-statistics"
LIVE_TABLES_PATH = ("/government/statistical-data-sets/"
                    "live-tables-on-dwelling-stock-including-vacants")

UA = {"User-Agent": "ucws-pipeline/s22 (+sl@slendeavours.org)"}


def _get_json(path):
    req = urllib.request.Request(API + path, headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def discover_council_taxbase():
    coll = _get_json(COLLECTION_PATH)
    links = []
    for group in coll.get("links", {}).get("documents", []):
        title = group.get("title", "")
        m = re.match(r"Council Taxbase (\d{4}) in England\s*$", title)
        if m:
            links.append((int(m.group(1)), title, group["base_path"],
                          group.get("public_updated_at")))
    if not links:
        halt("no 'Council Taxbase <year> in England' release found on "
             f"{COLLECTION_PATH} — landing page structure has changed")
    links.sort(reverse=True)
    year, title, base_path, _ = links[0]

    rel = _get_json(base_path)
    attachments = rel["details"].get("attachments", [])
    data_file = None
    for a in attachments:
        t = a.get("title", "")
        if re.search(r"local authority level data", t, re.I) and a.get("url"):
            data_file = a
            break
    if data_file is None:
        halt(f"no local-authority-level data file attached to '{title}' — "
             f"attachments seen: {[a.get('title') for a in attachments]}")

    tech = next((a for a in attachments
                 if re.search(r"technical note", a.get("title", ""), re.I)),
                None)
    tech_url = tech.get("url") if tech else None
    if tech_url and tech_url.startswith("/"):
        tech_url = "https://www.gov.uk" + tech_url

    dest = RAW_DIR / Path(data_file["url"]).name
    _download(data_file["url"], dest)
    return { ... release_title, taxbase_year, first_published,
             public_updated, url, content_type, file_size, path,
             technical_notes_url, all_attachments ... }


def discover_table_615():
    page = _get_json(LIVE_TABLES_PATH)
    att = None
    for a in page["details"].get("attachments", []):
        if re.match(r"Table 615\b", a.get("title", "")) and a.get("url"):
            att = a
            break
    if att is None:
        halt("Table 615 not found on the live tables on dwelling stock "
             "landing page — page structure has changed")
    dest = RAW_DIR / Path(att["url"]).name
    _download(att["url"], dest)
    return { ... attachment_title, url, content_type, file_size,
             public_updated, path ... }
```

## Logic (step by step)

1. Fetch the Council Taxbase statistics collection through the GOV.UK content API, which is the machine-readable form of the same landing page a person would read.
2. Filter the linked documents to titles matching `Council Taxbase <year> in England` exactly. Sort descending and take the highest year. Nothing is hardcoded to 2025.
3. Fetch that release's own content record and read its attachment list.
4. Select the attachment whose title contains "local authority level data". This is the LA-level workbook. Halt with the full attachment list if it is absent.
5. Separately select the technical notes attachment. Its URL is the citation for both structural breaks.
6. Download the workbook to `data/raw/s22_ctb/`, skipping the download if the file is already present and non-empty.
7. Fetch the dwelling stock live tables page and select the attachment whose title starts `Table 615`. Halt if absent.
8. Download Table 615 to the same directory.
9. Write the structure report, recording resolved URLs, formats, sizes, publication and revision dates, sheet names, header row positions, column headers as published, and the national headline figures printed on the release page.

## Behaviour

Re-run safe. Downloads are skipped when the file already exists, so a re-run costs two API calls. If MHCLG publishes a newer release the discovery picks it up automatically and downloads the new file under its own name; the previous file is left in place.

Two halt conditions, both explicit: no matching release on the collection page, and no local-authority-level data file on the release page. Neither is silently worked around.

## Connection

- Input: manual trigger
- Output: Node 2 (Extract Council Taxbase), Node 3 (Extract Table 615)

## Verified Output

2026-08-13. Resolved *Council Taxbase 2025 in England*, first published 2025-11-06, revised 2026-01-21, attachment "Council Taxbase: Local authority level data for 2025 (revised)", 1,800,242 bytes. Resolved "Table 615: vacant dwellings by local authority district: England, from 2004", 311,603 bytes, landing page last updated 2026-06-25. Structure report written to `build_reports/s22_source_structure.md`.
