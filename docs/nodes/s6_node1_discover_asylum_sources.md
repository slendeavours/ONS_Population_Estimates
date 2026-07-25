# Node 1 — Discover Asylum Sources

## Type
HTTP fetch + HTML link extraction

## Purpose
Locate the current download URL and edition label for `Asy_D11`, `Reg_02` and
`Asy_D09` from their GOV.UK landing pages at run time. GOV.UK asset URLs change
with every quarterly release, so no URL is hardcoded anywhere in the build.

## URL
```
https://www.gov.uk/government/statistical-data-sets/immigration-system-statistics-data-tables
https://www.gov.uk/government/statistical-data-sets/immigration-system-statistics-regional-and-local-authority-data
```

## Code
```python
def _discover(landing_url, link_pattern, extension):
    resp = requests.get(landing_url, timeout=60)
    resp.raise_for_status()
    hits = re.findall(
        r'<a[^>]+href="([^"]+\.%s)"[^>]*>(.*?)</a>' % extension,
        resp.text, re.I | re.S)
    matches = []
    for href, text in hits:
        label = re.sub(r"<[^>]+>", "", text).strip()
        if re.search(link_pattern, label, re.I):
            edition = re.search(r"year ending\s+(\w+\s+\d{4})", label, re.I)
            matches.append((href, label, edition.group(1) if edition else None))
    if not matches:
        sys.exit(f"HARD STOP: no {extension} link matching {link_pattern!r} "
                 f"on {landing_url}")
    matches.sort(key=key, reverse=True)   # newest parseable edition first
```

## Logic
1. Fetch each landing page.
2. Extract every anchor pointing at the required extension (`xlsx` or `ods`).
3. Strip inline markup from the link text so the label matches cleanly.
4. Filter to links whose label matches the table's search pattern.
5. Parse the `year ending <MONTH YEAR>` label out of each match.
6. Sort by parsed edition date descending and take the newest. Where the landing
   page serves stale and current editions simultaneously, this prefers the most
   recent `year ending` label rather than page order.
7. Hard stop if no link matches — a silent miss would load nothing.

## Query Parameters

| Parameter | Value |
|---|---|
| `link_pattern` (Asy_D11) | `Asylum seekers in receipt of Home Office support by local authority` |
| `link_pattern` (Asy_D09) | `Asylum seekers in receipt of Home Office support detailed` |
| `link_pattern` (Reg_02) | `Regional and local authority data on immigration groups` |
| `extension` | `xlsx` for Asy_D11 and Asy_D09, `ods` for Reg_02 |
| `timeout` | 60 s |

## Behaviour
- Read-only. No database contact.
- Re-run safe: discovery is idempotent and has no side effects.
- Edition label flows through to `source_edition` on every loaded row and into
  the `pipeline_run_log` note, so the loaded edition is always recoverable.
- If the Home Office renames a link, the build stops rather than silently
  loading the wrong table.

## Connection
Input: none.
Output: `{d11, d09, reg} → (url, edition_label)` passed to Node 2.

## Verified Output
- Asy_D11 — year ending March 2026 — `support-local-authority-datasets-mar-2026.xlsx`
- Asy_D09 — year ending March 2026 — `asylum-seekers-receipt-support-datasets-mar-2026.xlsx`
- Reg_02 — year ending March 2026 — `regional-and-local-authority-dataset-mar-2026.ods`
- Verified 2026-07-25 (initial build)
