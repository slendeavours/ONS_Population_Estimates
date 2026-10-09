"""Check registered sources for a new edition, and record every attempt.

Writes one source_check_log row per source per run, then updates
source_registry.last_check_at and last_seen_fingerprint.

A check that could not establish whether the source is current is recorded as
check_failed, never as no_change. Reaching a page proves the page exists; it
does not prove the data behind it is the data already loaded. The two are only
collapsed when a detected edition is actually compared against
latest_period_loaded, or a fingerprint against the stored one.

Detection patterns are per source and come from that source's node
documentation, not from guessing at a URL shape.

Usage:
    python scripts/check_sources.py                  # all eligible sources
    python scripts/check_sources.py 11 18 9a 9b      # named sources
    python scripts/check_sources.py --dry-run        # fetch, log nothing
"""
import argparse
import datetime
import hashlib
import json
import re
import sys
import urllib.error
import urllib.parse
import os
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _db import ENV, get_conn  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

UA = {"User-Agent": "ucws-pipeline/source-check (+sl@slendeavours.org)"}
TIMEOUT = 45

MONTHS = ("january february march april may june july august september "
          "october november december").split()

# Per-source detection. 'pattern' finds candidate links on the landing page;
# 'period' turns the winning link into a comparable edition label.
#
# check_method is 'landing_page' for all four of these. None is a GOV.UK
# publication, so none exercises the govuk_content_api path — that route is
# used by S6, S15 and S22 and remains untested by this pass.
DETECTORS = {
    "11": dict(
        method="landing_page",
        pattern=r'href="([^"]*HSCA_Active_Locations[^"]*\.(?:ods|xlsx))"',
        note="href whose filename contains HSCA_Active_Locations (.ods or "
             ".xlsx); docs/nodes/s11_node1_fetch_cqc_directory.md",
    ),
    # S18's URL carries the publication date, not the reference period: the
    # 22 July 2026 edition contains data to June 2026. Comparing the two as
    # if they were the same kind of thing reports a new edition forever, so
    # the test is whether this edition's slug already appears in the target
    # table's own source column.
    "18": dict(
        method="landing_page",
        pattern=r'href="([^"]*\.xlsx)"',
        edition_key=r"/(\d{1,2}[a-z]+\d{4})/",
        note="first (newest) xlsx link on the dataset page; "
             "docs/s18_pipr_source.md",
    ),
    # S9a detects through GOV.UK and takes files from NHS England. Each month
    # is published as an official statistic, so the release page is a cleaner
    # signal than the file list — and it is the one GOV.UK route among the
    # sources that are currently due.
    "9a": dict(
        method="govuk_content_api",
        govuk_query="Timeliness of Acute Hospital Discharges Discharge Ready Date",
        govuk_title=r"discharge ready date\)? for ([a-z]+) (\d{4})",
        file_pattern=r'href="([^"]*Discharge-Ready-Date[^"]*\.xlsx)"',
        note="GOV.UK official statistics releases for detection; "
             "NHS England discharge-ready-date page for files",
    ),
    # --- GOV.UK collection detectors, added by the tier-C mechanics pass ---
    # The collection is the stable object; release pages change per edition.
    "1": dict(method="govuk_content_api",
              collection="homelessness-statistics",
              title_filter=r"^Statutory homelessness in England: \w+ to \w+ \d{4}$",
              note="Homelessness statistics collection, quarterly statutory "
                   "homelessness releases"),
    "2": dict(method="govuk_content_api",
              collection="local-authority-revenue-expenditure-and-financing",
              title_filter=r"outturn",
              note="RO4 outturn releases (not the budget releases) in the "
                   "revenue expenditure and financing collection"),
    "5": dict(method="govuk_content_api",
              collection="english-indices-of-deprivation",
              title_filter=r"English indices of deprivation \d{4}$",
              note="English indices of deprivation collection"),
    "10": dict(method="govuk_content_api",
               collection="homelessness-statistics",
               title_filter=r"^Rough sleeping snapshot in England: autumn",
               note="Rough sleeping snapshot releases in the Homelessness "
                    "statistics collection"),
    "13": dict(method="govuk_content_api",
               collection="local-authority-housing-data",
               title_filter=r"housing statistics data returns",
               note="LAHS data returns in the Local authority housing data "
                    "collection"),
    # --- Stat-Xplore detectors. The date field's newest member is the
    # newest published month; no table query and no download needed.
    "8": dict(method="api_probe",
              date_field="str:field:hb_new:F_HB_NEW_DATE:NEW_DATE_NAME",
              note="Stat-Xplore hb_new Month field"),
    "8b": dict(method="api_probe",
               date_field="str:field:hb_new:F_HB_NEW_DATE:NEW_DATE_NAME",
               note="Stat-Xplore hb_new Month field"),
    "19": dict(method="api_probe",
               date_field="str:field:PIP_Monthly_new:F_PIP_DATE:DATE2",
               note="Stat-Xplore PIP_Monthly_new Month field"),
    "9b": dict(
        method="landing_page",
        pattern=r'href="([^"]*/performance-[a-z]+-\d{4}[^"]*)"',
        note="performance-{month}-{year} publication pages; "
             "docs/nodes/s9b_node1_fetch_mhsds_monthly.md",
    ),
    # --- Finder detectors. These three sources had no detector, so every run
    # reported check_failed ("mechanics not documented") even though the
    # registry documents how each is published. Each finder returns the newest
    # edition the publisher offers as 'YYYY-MM' or 'YYYY-MM-DD'; the loaded side
    # comes from the target table itself (loaded_sql), or from a stated
    # constant where the table does not record its own vintage.
    "15": dict(method="govuk_content_api", finder="s15",
               loaded_sql="SELECT to_char(MAX(period), 'YYYY-MM') FROM la_house_prices",
               note="UK HPI monthly release titles in the UK House Price Index "
                    "reports collection; the title month is the data month"),
    "24": dict(method="govuk_content_api", finder="s24",
               loaded_sql="SELECT MAX(snapshot_date)::text FROM rsh_registered_providers",
               note="'List of registered providers' attachment titles on the RSH "
                    "register page (GOV.UK content API); snapshot date is in the title"),
    # la_boundaries does not record its own vintage, so the loaded side is
    # stated here: LAD May 2024 BGC (registry known_gotchas for S7). A newer
    # vintage is reported, never adopted: changing vintage re-bases every join.
    # --- The remaining twelve sources. Each reads the publisher's own release listing; where a source
    # cannot be detected from outside (S20) or only half can (S12), the note says so.
    "1b": dict(method="govuk_content_api", finder="s1b",
               loaded_sql="SELECT MAX(period) FROM la_homelessness_support_needs",
               note="Same quarterly release as S1 (Table A3 is a sheet in the detailed LA tables)"),
    "3": dict(method="api_probe", finder="s3",
              loaded_sql="SELECT MAX(reference_year)::text FROM la_population",
              note="ONS MYE dataset listing (the /data JSON the registry api_endpoint points at), newest "
                   "mid<year> edition, as s3_mye_refresh.py resolves it"),
    "3b": dict(method="api_probe", finder="s3b", loaded_const="2023-01-05",
               note="NOMIS dataset NM_2072_1 (TS054) LastUpdated. Census 2021 is static, so the baseline is the "
                    "publisher's LastUpdated seen on 2026-10-01 and any later value is reported"),
    "4": dict(method="api_probe", finder="s4",
              note="DfE Explore Education Statistics content API, latest release of Children looked after in England"),
    "6": dict(method="govuk_content_api", finder="s6",
              note="Home Office immigration system statistics data tables, newest 'year ending <month year>' in the change history"),
    "12": dict(method="govuk_content_api", finder="s12",
               note="EFS half only: 'Exceptional Financial Support ... for <year>' documents in the GOV.UK collection. "
                    "S.114 notices are issued by individual councils and cannot be watched from here"),
    "14": dict(method="govuk_content_api", finder="s14",
               note="'Universal Credit Local Housing Allowance rates: <year> to <year>' publication on GOV.UK"),
    "17": dict(method="landing_page", finder="s17",
               note="Newest YYYY-YY label on the SafeLives latest MARAC data page; the page is the only signal"),
    "20": dict(method="manual", finder="s20",
               note="Private rate card, supplied by Scott when he gets a newer copy; nothing is published. Detects "
                    "only a newer s20_ratecard_rates_<date>.csv in the repo or data/reference than the registry's "
                    "latest_period_loaded"),
    "21": dict(method="api_probe", finder="s21", loaded_norm="month_year",
               note="ONS dataset page releaseDate for the nearest-neighbours workbook"),
    "22": dict(method="govuk_content_api", finder="s22",
               note="'Council Taxbase <year> in England' releases in the council taxbase statistics collection"),
    "23": dict(method="govuk_content_api", finder="s23",
               note="'Registered provider social housing stock and rents in England <y1> to <y2>' in the RSH collection; "
                    "stock is at 31 March of the second year"),
    "7": dict(method="api_probe", finder="s7", loaded_const="2024-05",
              note="ArcGIS REST service list on the Open Geography Portal, "
                   "Local_Authority_Districts_*_BGC services; adopting a new "
                   "vintage is a deliberate decision, never a routine refresh"),
}


MONTH_RE = "|".join(MONTHS)

# --- GOV.UK collection title parsers -------------------------------------
# Each returns a period string in the same shape the target table stores, or
# None. None is a refusal: the checker reports check_failed rather than
# comparing something it could not normalise.
#
# These are per source because publishers label periods their own way, and a
# generic parser would guess. S1 is the clearest case: "January to March 2026"
# is financial-year quarter 2025Q4, not 2026Q1, and getting that wrong would
# report a loaded quarter as missing every month.

FIN_QUARTER = {"april": ("Q1", 0), "july": ("Q2", 0),
               "october": ("Q3", 0), "january": ("Q4", -1)}


def title_period_s1(title):
    """'Statutory homelessness in England: October to December 2025' -> 2025Q3."""
    # The second month must be a non-capturing group. Interpolated bare, the
    # alternation binds looser than the surrounding literals and the pattern
    # silently becomes "(months) to january" OR "february" OR ... — which
    # matches nothing useful and reported 40 releases as unparseable.
    m = re.search(rf"({MONTH_RE}) to (?:{MONTH_RE}) (\d{{4}})", title.lower())
    if not m or m.group(1) not in FIN_QUARTER:
        return None
    q, offset = FIN_QUARTER[m.group(1)]
    return f"{int(m.group(2)) + offset}{q}"


def title_period_s10(title):
    """'Rough sleeping snapshot in England: autumn 2025' -> 2025."""
    m = re.search(r"autumn (\d{4})", title.lower())
    return m.group(1) if m else None


def title_period_s2(title):
    """RO4 outturn: '... England: 2024 to 2025 ... outturn' -> 2024-25."""
    if "outturn" not in title.lower():
        return None            # budget releases are not the RO4 outturn
    m = re.search(r"(\d{4}) to (\d{4})", title)
    return f"{m.group(1)}-{m.group(2)[2:]}" if m else None


def title_period_s5(title):
    """'English indices of deprivation 2019' -> 2019."""
    m = re.search(r"english indices of deprivation (\d{4})", title.lower())
    return m.group(1) if m else None


def title_period_s13(title):
    """'... data returns for 2020 to 2021' -> 2021 (the reporting year)."""
    m = re.search(r"returns for (\d{4}) to (\d{4})", title.lower())
    return m.group(2) if m else None


TITLE_PARSERS = {
    "1": title_period_s1, "2": title_period_s2, "5": title_period_s5,
    "10": title_period_s10, "13": title_period_s13,
}


def parse_period(url):
    """(year, month, day) from a file or publication URL, or None.

    Separators vary by publisher and several use none at all: CQC writes
    01_July_2026, ONS writes 22july2026 in the path, NHS England writes
    September2023 in the filename. A pattern that assumes a delimiter misses
    two of the three.

    None is a real answer and is treated as "could not establish", never as
    "unchanged".
    """
    u = url.lower()
    pats = [
        (rf"(\d{{1,2}})[_\-]?({MONTH_RE})[_\-]?(\d{{4}})", ("d", "m", "y")),
        (rf"({MONTH_RE})[_\-]?(\d{{4}})", ("m", "y")),
        (r"(\d{4})[-_](\d{2})(?!\d)", ("y", "mn")),
    ]
    for pat, order in pats:
        m = re.search(pat, u)
        if not m:
            continue
        g = dict(zip(order, m.groups()))
        year = int(g["y"])
        month = int(g["mn"]) if "mn" in g else MONTHS.index(g["m"]) + 1
        day = int(g["d"]) if "d" in g else 0
        return (year, month, day)
    return None


def format_period(parsed):
    if parsed is None:
        return None
    y, m, d = parsed
    return f"{y:04d}-{m:02d}-{d:02d}" if d else f"{y:04d}-{m:02d}"


def pick_newest(links, today):
    """The newest link by parsed period, not by position on the page.

    Page order is not release order: NHS England and NHS Digital both list
    oldest first, and taking links[0] there returns a 2023 edition. Where
    nothing parses, document order is the only remaining signal.

    Periods after the current month are excluded. NHS Digital publishes
    scheduled publication pages ahead of the data, and a page for next month
    is a calendar entry, not an edition that exists.
    """
    dated = []
    for link in links:
        p = parse_period(link)
        if p and (p[0], p[1]) <= (today[0], today[1]):
            dated.append((p, link))
    if dated:
        best = max(dated, key=lambda t: t[0])
        return best[1], best[0], len(dated)
    return links[0], None, 0


def fetch(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.status, r.read().decode("utf-8", errors="replace")


def govuk_releases(query, title_pattern):
    """Published GOV.UK statistics releases matching a query.

    Returns [(period, title, link, published)], newest first. Detection here
    is on the release, not the file: GOV.UK announces the statistic and NHS
    England hosts the workbook, so the release page is the earlier and more
    reliable signal of a new month.
    """
    url = ("https://www.gov.uk/api/search.json?q="
           + urllib.parse.quote(query)
           + "&count=30&filter_format=official_statistics"
           + "&fields=title,link,public_timestamp")
    status, body = fetch(url)
    out = []
    for r in json.loads(body).get("results", []):
        title = r.get("title") or ""
        m = re.search(title_pattern, title.lower())
        if not m or m.group(1) not in MONTHS:
            continue
        out.append(((int(m.group(2)), MONTHS.index(m.group(1)) + 1, 0),
                    title, r.get("link"), (r.get("public_timestamp") or "")[:10]))
    out.sort(key=lambda t: t[0], reverse=True)
    return status, out


def govuk_collection(slug, title_filter):
    """Releases listed in a GOV.UK collection, newest publication first.

    The collection is the stable thing: individual release pages come and go
    per edition, the collection does not. links.documents carries the title,
    path and publication date for every release in it.
    """
    url = f"https://www.gov.uk/api/content/government/collections/{slug}"
    status, body = fetch(url)
    d = json.loads(body)
    out = []
    for doc in (d.get("links", {}) or {}).get("documents", []) or []:
        title = doc.get("title") or ""
        if title_filter and not re.search(title_filter, title, re.I):
            continue
        out.append((title, doc.get("base_path"),
                    (doc.get("public_updated_at") or "")[:10]))
    out.sort(key=lambda t: t[2], reverse=True)
    return status, out



def statxplore_latest(date_field_id):
    """The newest period offered by a Stat-Xplore date field.

    Members come back in chronological order, so the newest is the last one.
    That ordering is used deliberately instead of comparing labels: a label
    such as 'Apr-26' (S19 stored these until 2026-10-08) would rank 'Mar-26'
    above it as a string.
    Position is the publisher's own ordering; string order is our assumption.
    """
    key = (os.environ.get("StatXplore_API_Key")
           or ENV.get("StatXplore_API_Key")
           or os.environ.get("STATXPLORE_API_KEY") or "").strip()
    if not key:
        return None, None
    root = "https://stat-xplore.dwp.gov.uk/webapi/rest/v1"
    hdr = dict(UA)
    hdr.update({"APIKey": key, "Content-Type": "application/json"})

    def get(sid):
        req = urllib.request.Request(
            f"{root}/schema/{sid.replace(':', '%3A')}", headers=hdr)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, json.loads(r.read())

    status, field = get(date_field_id)
    vs = field.get("children") or []
    if not vs:
        return status, None
    status, valueset = get(vs[0]["id"])
    members = valueset.get("children") or []
    return status, (members[-1].get("label") if members else None)


def check_statxplore(row, det, reg):
    try:
        status, newest = statxplore_latest(det["date_field"])
        row["http_status"] = status
    except Exception as e:
        row.update(outcome="check_failed",
                   error_detail=f"{type(e).__name__}: {str(e)[:180]}")
        return row
    if not newest:
        row.update(outcome="check_failed",
                   error_detail=("no credential, or the date field returned "
                                 "no members. StatXplore_API_Key must be set "
                                 "for this check to run."))
        return row

    # '202602 (Feb-26)' -> '202602'; a label such as 'Apr-26' stays as it is.
    m = re.match(r"^(\d{6})", newest)
    period = m.group(1) if m else newest
    row["detected_period"] = period
    row["fingerprint_after"] = hashlib.sha256(period.encode()).hexdigest()[:32]
    row["notes"] = f"{det['note']}; newest member '{newest}'"

    loaded = (reg["latest_period_loaded"] or "").strip()
    if not loaded:
        row.update(outcome="check_failed",
                   error_detail=("newest period found but "
                                 "latest_period_loaded is not set"))
        return row
    # Equality, not ordering. The API lists chronologically, so anything other
    # than the newest member means behind; it cannot mean ahead.
    row["outcome"] = "no_change" if period == loaded else "new_edition"
    return row


def check_govuk_collection(row, det, reg):
    """Detect a new edition from a GOV.UK collection listing."""
    code = row["source_code"]
    try:
        status, releases = govuk_collection(det["collection"],
                                            det.get("title_filter"))
        row["http_status"] = status
    except Exception as e:
        row.update(outcome="check_failed",
                   error_detail=f"{type(e).__name__}: {str(e)[:180]}")
        return row
    if not releases:
        row.update(outcome="check_failed",
                   error_detail=(f"the collection resolved but no release "
                                 f"title matched "
                                 f"{det.get('title_filter')!r}; the "
                                 f"publisher has probably retitled the "
                                 f"series"))
        return row

    parser = TITLE_PARSERS.get(code)
    dated = [(parser(t), t, p, d) for t, p, d in releases] if parser else []
    dated = [x for x in dated if x[0]]
    if not dated:
        row.update(
            outcome="check_failed",
            error_detail=(f"{len(releases)} release(s) found but no period "
                          f"could be normalised from their titles, so there "
                          f"is nothing comparable with latest_period_loaded"))
        return row

    newest = max(dated, key=lambda x: x[0])
    row["detected_period"] = newest[0]
    row["fingerprint_after"] = hashlib.sha256(
        newest[2].encode()).hexdigest()[:32]
    row["notes"] = (f"{det['note']}; {len(releases)} release(s) matched, "
                    f"{len(dated)} dated; newest '{newest[1][:70]}' "
                    f"published {newest[3]}")

    loaded = (reg["latest_period_loaded"] or "").strip()
    if not loaded:
        row.update(outcome="check_failed",
                   error_detail=("release found but latest_period_loaded is "
                                 "not set, so there was nothing to compare"))
        return row
    if reg.get("detected_period_type") != "reference_period":
        row.update(outcome="check_failed",
                   error_detail=("detected_period_type is not "
                                 "reference_period, so the detected period "
                                 "is not comparable with what is loaded"))
        return row
    row["outcome"] = "new_edition" if newest[0] > loaded else "no_change"
    return row


def loaded_provenance(cur, table):
    """Per-period source filenames already loaded, where the table records them.

    Only some target tables carry a source column. Where one exists it is the
    strongest revision check available: it says which file each loaded period
    actually came from, so a republished file is visible without downloading
    anything or trusting a fingerprint.
    """
    cur.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s AND column_name='source'
    """, (table,))
    if not cur.fetchone():
        return None
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s
          AND column_name = ANY(%s)
        LIMIT 1
    """, (table, list(PERIOD_COLUMNS)))
    row = cur.fetchone()
    if not row:
        return None
    col = row[0]
    cur.execute(f'SELECT "{col}"::text, MIN(source) FROM "{table}" GROUP BY 1')
    return {p: (s or "").rsplit("/", 1)[-1] for p, s in cur.fetchall()}


PERIOD_COLUMNS = ("reporting_period", "period_ending", "period", "month",
                  "taxbase_year", "snapshot_year", "reference_year",
                  "financial_year", "reporting_year", "year")



def edition_loaded(table, edition):
    """Whether an edition identifier already appears in the table's source column.

    Returns None where the question cannot be asked - no table, no source
    column - so the caller can report check_failed rather than guess.
    """
    if not table or not edition:
        return None
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT 1 FROM information_schema.columns
            WHERE table_schema='public' AND table_name=%s
              AND column_name='source'
        """, (table,))
        if not cur.fetchone():
            return None
        cur.execute(f'SELECT EXISTS (SELECT 1 FROM "{table}" '
                    f'WHERE source ILIKE %s)', (f"%{edition}%",))
        return bool(cur.fetchone()[0])
    finally:
        cur.close()
        conn.close()


def _month_year(text):
    """('YYYY-MM', month, year) from the first 'Month YYYY' in text, else None."""
    abbr = [mo[:3] for mo in MONTHS]
    for m in re.finditer(r"([a-z]{3,9})\W+(\d{4})", text.lower()):
        word = m.group(1)
        if word[:3] in abbr and (word in MONTHS or len(word) == 3):
            return f"{int(m.group(2)):04d}-{abbr.index(word[:3]) + 1:02d}"
    return None


def find_s15():
    """Newest 'UK House Price Index for <Month> <Year>' release."""
    status, body = fetch("https://www.gov.uk/api/content/government/collections/"
                         "uk-house-price-index-reports")
    docs = (json.loads(body).get("links", {}) or {}).get("documents", []) or []
    hits = [(_month_year(d["title"]), d["title"], (d.get("public_updated_at") or "")[:10])
            for d in docs if re.match(r"^UK House Price Index for \w+ \d{4}$", d.get("title", ""))]
    hits = [h for h in hits if h[0]]
    if not hits:
        raise ValueError("no 'UK House Price Index for <Month> <Year>' title in the collection; "
                         "the publisher has probably retitled the series")
    best = max(hits)
    return status, best[0], f"'{best[1]}' published {best[2]}"


def find_s24():
    """Newest dated 'List of registered providers' attachment on the RSH page."""
    status, body = fetch("https://www.gov.uk/api/content/government/publications/"
                         "registered-providers-of-social-housing")
    atts = (json.loads(body).get("details", {}) or {}).get("attachments", []) or []
    dates = []
    for a in atts:
        t = a.get("title") or ""
        m = re.search(rf"list of registered providers\W+(\d{{1,2}}) ({MONTH_RE}) (\d{{4}})", t.lower())
        if m:
            dates.append((f"{int(m.group(3)):04d}-{MONTHS.index(m.group(2)) + 1:02d}-{int(m.group(1)):02d}", t))
    if not dates:
        raise ValueError("no dated 'List of registered providers' attachment on the page")
    best = max(dates)
    return status, best[0], f"'{best[1][:70]}'"


def find_s7():
    """Newest Local Authority Districts BGC (UK) vintage in the ArcGIS service list."""
    status, body = fetch("https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services?f=json")
    vint = []
    for s in json.loads(body).get("services", []):
        n = s.get("name", "")
        if re.match(r"^Local_Authority_Districts", n, re.I) and "BGC" in n.upper() and "UK" in n.upper():
            v = _month_year(n.replace("_", " ").replace("(", " ").replace(")", " "))
            if v:
                vint.append((v, n))
    if not vint:
        raise ValueError("no Local_Authority_Districts *_BGC service in the service list")
    best = max(vint)
    return status, best[0], f"service '{best[1]}'; {len(set(v for v, _ in vint))} vintages offered"


def _content(path):
    status, body = fetch("https://www.gov.uk/api/content" + path)
    return status, json.loads(body)


def _collection_docs(slug):
    status, d = _content(f"/government/collections/{slug}")
    return status, (d.get("links", {}) or {}).get("documents", []) or []


def find_s1b():
    status, rel = govuk_collection("homelessness-statistics",
                                   r"^Statutory homelessness in England: \w+ to \w+ \d{4}$")
    per = [(title_period_s1(t), t) for t, _, _ in rel if title_period_s1(t)]
    if not per:
        raise ValueError("no statutory homelessness quarter title could be parsed")
    best = max(per)
    return status, best[0], f"'{best[1][:70]}'"


def find_s3():
    base = "https://www.ons.gov.uk"
    land = ("/peoplepopulationandcommunity/populationandmigration/populationestimates/datasets/"
            "estimatesofthepopulationforenglandandwales")
    status, body = fetch(base + land + "/data")
    ed = []
    for d in json.loads(body).get("datasets", []):
        m = re.search(r"/mid(\d{4})(\d{4})localauthorityboundaries$", d.get("uri", ""))
        if m:
            ed.append((m.group(1), d["uri"]))
    if not ed:
        raise ValueError("no mid<year><boundary>localauthorityboundaries edition on the ONS listing")
    best = max(ed)
    return status, best[0], f"edition {best[1].rsplit('/', 1)[-1]}"


def find_s3b():
    status, body = fetch("https://www.nomisweb.co.uk/api/v01/dataset/def.sdmx.json?search=*TS054*")
    for k in json.loads(body)["structure"]["keyfamilies"]["keyfamily"]:
        if k["id"] == "NM_2072_1":
            for a in k.get("annotations", {}).get("annotation", []):
                if a.get("annotationtitle") == "LastUpdated":
                    return status, a["annotationtext"][:10], "NOMIS NM_2072_1 LastUpdated"
    raise ValueError("NM_2072_1 or its LastUpdated annotation not found")


def find_s4():
    status, body = fetch("https://content.explore-education-statistics.service.gov.uk/api/publications/"
                         "children-looked-after-in-england-including-adoptions")
    d = json.loads(body)
    lr = d.get("latestRelease") or {}
    m = re.search(r"(\d{4})", lr.get("slug", "") or lr.get("title", ""))
    if not m:
        raise ValueError(f"latestRelease carries no year: {lr}")
    return status, m.group(1), f"'{lr.get('title')}', next release {d.get('nextReleaseDate')}"


def find_s6():
    status, d = _content("/government/statistical-data-sets/immigration-system-statistics-data-tables")
    notes = " ".join(c.get("note", "") for c in d.get("details", {}).get("change_history", []))
    ends = []
    for mon, yr in re.findall(rf"year ending ({MONTH_RE}) (\d{{4}})", notes.lower()):
        m = MONTHS.index(mon) + 1
        last = datetime.date(int(yr) + (m == 12), m % 12 + 1, 1) - datetime.timedelta(days=1)
        ends.append(last.isoformat())
    if not ends:
        raise ValueError("no 'year ending <month year>' in the change history")
    return status, max(ends), f"change history updated {d.get('public_updated_at', '')[:10]}"


def find_s12():
    status, docs = _collection_docs("exceptional-financial-support-for-local-authorities")
    yrs = [(f"{m.group(1)}-{m.group(2)}", x["title"], (x.get("public_updated_at") or "")[:10])
           for x in docs for m in [re.search(r"for (\d{4})-(\d{2})$", x.get("title", ""))] if m]
    if not yrs:
        raise ValueError("no 'Exceptional Financial Support ... for <year>' title in the collection")
    best = max(yrs)
    return status, best[0], f"'{best[1][:60]}' updated {best[2]}"


def find_s14():
    url = ("https://www.gov.uk/api/search.json?q=" + urllib.parse.quote("Universal Credit Local Housing Allowance rates")
           + "&count=20&fields=title,link,public_timestamp")
    status, body = fetch(url)
    yrs = [(f"{m.group(1)}-{m.group(2)[2:]}", r["title"], r["public_timestamp"][:10])
           for r in json.loads(body).get("results", [])
           for m in [re.match(r"^Universal Credit Local Housing Allowance rates: (\d{4}) to (\d{4})$", r.get("title", ""))] if m]
    if not yrs:
        raise ValueError("no 'Universal Credit Local Housing Allowance rates: <y> to <y>' result")
    best = max(yrs)
    return status, best[0], f"'{best[1]}' published {best[2]}"


def find_s17():
    status, body = fetch("https://safelives.org.uk/practice-support/resources-marac-meetings/latest-marac-data/")
    text = re.sub(r"<[^>]+>", " ", body)
    labels = re.findall(r"\b(20\d\d-\d\d)\b", text)
    if not labels:
        raise ValueError("no YYYY-YY year label on the SafeLives page; the layout has probably changed")
    return status, max(labels), f"{len(set(labels))} year labels on the page"


def find_s20():
    here = Path(__file__).resolve().parent.parent
    dates = []
    for d in (here, here / "data" / "reference"):
        for f in d.glob("s20_ratecard_rates_*.csv"):
            m = re.search(r"(\d{4}-\d{2}-\d{2})", f.name)
            if m:
                dates.append((m.group(1), f.name))
    if not dates:
        raise ValueError("no s20_ratecard_rates_<date>.csv file found")
    best = max(dates)
    return 200, best[0], f"newest file supplied: {best[1]}"


def find_s21():
    status, body = fetch("https://www.ons.gov.uk/peoplepopulationandcommunity/wellbeing/datasets/"
                         "clusteringsimilarlocalauthoritiesandstatisticalnearestneighboursintheuk/data")
    rd = (json.loads(body).get("description", {}) or {}).get("releaseDate", "")[:10]
    if not rd:
        raise ValueError("no releaseDate on the ONS dataset page")
    return status, rd[:7], f"releaseDate {rd}"


def find_s22():
    status, docs = _collection_docs("council-taxbase-statistics")
    yrs = [(m.group(1), x["title"], (x.get("public_updated_at") or "")[:10]) for x in docs
           for m in [re.match(r"^Council Taxbase (\d{4}) in England$", x.get("title", ""))] if m]
    if not yrs:
        raise ValueError("no 'Council Taxbase <year> in England' title in the collection")
    best = max(yrs)
    later = [x["title"] for x in docs
             if re.match(r"Council Taxbase", x.get("title", ""))
             and not re.match(r"^Council Taxbase (\d{4}) in England$", x["title"])
             and any(y > best[0] for y in re.findall(r"(\d{4})", x["title"]))]
    if later:
        raise ValueError(f"newest matching title is {best[0]} but the collection lists "
                         f"{later} with a later year that does not fit the title pattern")
    return status, best[0], f"'{best[1]}' updated {best[2]}"


def find_s23():
    status, docs = _collection_docs("registered-provider-social-housing-stock-and-rents-in-england")
    eds = [(f"{m.group(2)}-03-31", x["title"], (x.get("public_updated_at") or "")[:10]) for x in docs
           for m in [re.search(r"England (\d{4}) to (\d{4})$", x.get("title", ""))] if m]
    if not eds:
        raise ValueError("no 'stock and rents in England <y1> to <y2>' title in the collection")
    best = max(eds)
    return status, best[0], f"'{best[1][:70]}' updated {best[2]}"


FINDERS = {"s15": find_s15, "s24": find_s24, "s7": find_s7, "s1b": find_s1b, "s3": find_s3, "s3b": find_s3b,
           "s4": find_s4, "s6": find_s6, "s12": find_s12, "s14": find_s14, "s17": find_s17, "s20": find_s20,
           "s21": find_s21, "s22": find_s22, "s23": find_s23}


def check_finder(row, det, reg):
    """Compare the newest edition a finder sees with what the target table holds."""
    try:
        status, period, desc = FINDERS[det["finder"]]()
        row["http_status"] = status
    except Exception as e:
        row.update(outcome="check_failed",
                   error_detail=f"{type(e).__name__}: {str(e)[:200]}")
        return row
    row["detected_period"] = period
    row["fingerprint_after"] = hashlib.sha256(f"{period}|{desc}".encode()).hexdigest()[:32]
    if det.get("loaded_sql"):
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute(det["loaded_sql"])
            loaded = (cur.fetchone() or [None])[0]
        finally:
            conn.close()
    else:
        loaded = det.get("loaded_const") or (reg.get("latest_period_loaded") or "").strip() or None
    if loaded and det.get("loaded_norm") == "month_year":
        loaded = _month_year(str(loaded))
    row["notes"] = f"{det['note']}; newest {period} ({desc}); loaded {loaded}"
    if not loaded:
        row.update(outcome="check_failed",
                   error_detail="the target table holds no period to compare against")
        return row
    row["outcome"] = "new_edition" if period > str(loaded)[:len(period)] else "no_change"
    return row


def check_govuk(row, det, reg):
    """Detect via the GOV.UK release list rather than the publisher's files."""
    try:
        status, rels = govuk_releases(det["govuk_query"], det["govuk_title"])
        row["http_status"] = status
    except Exception as e:
        row.update(outcome="check_failed",
                   error_detail=f"{type(e).__name__}: {str(e)[:200]}")
        return row

    today = datetime.date.today().timetuple()[:3]
    rels = [r for r in rels if (r[0][0], r[0][1]) <= (today[0], today[1])]
    if not rels:
        row.update(outcome="check_failed",
                   error_detail="the GOV.UK search returned no release whose "
                                "title matched the documented pattern")
        return row

    period, title, link, published = rels[0]
    row["detected_period"] = format_period(period)
    row["fingerprint_after"] = hashlib.sha256(
        (link or title).encode()).hexdigest()[:32]
    row["notes"] = (f"{det['note']}; {len(rels)} release(s) matched; newest "
                    f"'{title}' published {published}")

    loaded = (reg["latest_period_loaded"] or "").strip()
    loaded_ym = loaded[:7]
    if reg.get("detected_period_type") != "reference_period":
        row.update(outcome="check_failed",
                   error_detail=(
                       "detected_period_type is not reference_period, so the "
                       "detected release month cannot be compared with "
                       "latest_period_loaded"))
    elif loaded_ym and row["detected_period"]:
        row["outcome"] = ("new_edition"
                          if row["detected_period"][:7] > loaded_ym
                          else "no_change")
    else:
        row.update(outcome="check_failed",
                   error_detail="release found but latest_period_loaded is "
                                "not set, so there was nothing to compare "
                                "against")

    # A revising source needs the second question asked as well: not only
    # "is there a newer period" but "has a period already loaded been
    # republished". A new edition does not mask an outstanding revision, so
    # the revision finding is appended to the notes either way, and it takes
    # the outcome when nothing newer has appeared.
    if reg.get("revises_back_series") and det.get("file_pattern"):
        superseded, checked = revision_check(det, reg)
        if checked is None:
            row["notes"] += "; revision check not possible"
        elif superseded:
            row["notes"] += (f"; {len(superseded)} loaded period(s) "
                             f"superseded: {', '.join(superseded[:6])}"
                             + (" ..." if len(superseded) > 6 else ""))
            if row["outcome"] == "no_change":
                row["outcome"] = "revision_detected"
            row["error_detail"] = (
                f"{len(superseded)} already-loaded period(s) have been "
                f"republished since they were loaded. Row counts and gates "
                f"are unaffected; the values are simply no longer what the "
                f"publisher says they are.")
        else:
            row["notes"] += (f"; revision check clean across {checked} "
                             f"loaded period(s)")
    return row


def revision_check(det, reg):
    """Loaded periods whose published filename no longer matches what was loaded.

    Uses the per-period source column the target table already records, so a
    republished file is detectable from the link list alone — the -Revised
    suffix on the DRD filenames is the whole signal, and nothing is
    downloaded.

    Returns (superseded_periods, periods_checked). (None, None) where the
    comparison cannot be made at all.
    """
    conn = get_conn()
    cur = conn.cursor()
    try:
        loaded = loaded_provenance(cur, reg["target_table"])
    finally:
        cur.close()
        conn.close()
    if not loaded:
        return None, None

    try:
        _, body = fetch(reg["landing_page_url"])
    except Exception:
        return None, None

    published = {}
    for link in re.findall(det["file_pattern"], body, re.I):
        p = format_period(parse_period(link))
        if p:
            published[p[:7]] = link.rsplit("/", 1)[-1]

    superseded, checked = [], 0
    for period, loaded_file in sorted(loaded.items()):
        current = published.get(period[:7])
        if not current or not loaded_file:
            continue
        checked += 1
        if current != loaded_file:
            superseded.append(f"{period[:7]} ({loaded_file[-28:]} -> "
                              f"{current[-28:]})")
    return superseded, checked


def check_one(code, reg):
    """Returns the source_check_log row fields for one source."""
    det = DETECTORS.get(code)
    row = dict(source_code=code, check_method=(det or {}).get("method", "manual"),
               fingerprint_before=reg["last_seen_fingerprint"],
               fingerprint_after=None, detected_period=None,
               http_status=None, error_detail=None,
               notes=(det or {}).get("note"))

    if not det:
        row.update(outcome="check_failed",
                   error_detail="no detection pattern is defined for this "
                                "source; its acquisition mechanics are not "
                                "documented")
        return row
    # Routing comes before the landing-page guard: an API-probe or collection
    # detector needs no landing page, and requiring one would fail a check
    # that has everything it needs.
    if det.get("finder"):
        return check_finder(row, det, reg)
    if det.get("date_field"):
        return check_statxplore(row, det, reg)
    if det.get("collection"):
        return check_govuk_collection(row, det, reg)
    if det.get("govuk_query"):
        return check_govuk(row, det, reg)

    if not reg["landing_page_url"]:
        row.update(outcome="check_failed",
                   error_detail="no landing_page_url in the registry")
        return row

    try:
        status, body = fetch(reg["landing_page_url"])
        row["http_status"] = status
    except urllib.error.HTTPError as e:
        row.update(outcome="check_failed", http_status=e.code,
                   error_detail=f"HTTPError {e.code} fetching the landing page")
        return row
    except Exception as e:
        row.update(outcome="check_failed",
                   error_detail=f"{type(e).__name__}: {str(e)[:200]}")
        return row

    links = re.findall(det["pattern"], body, re.I)
    if not links:
        row.update(outcome="check_failed",
                   error_detail="landing page reached but no link matched the "
                                "documented pattern. The page layout has "
                                "probably changed; this is not evidence the "
                                "source is unchanged.")
        return row

    today = datetime.date.today().timetuple()[:3]
    newest, parsed, n_dated = pick_newest(links, today)
    row["fingerprint_after"] = hashlib.sha256(newest.encode()).hexdigest()[:32]
    row["detected_period"] = format_period(parsed)
    row["notes"] = (f"{det['note']}; {len(links)} link(s) matched, "
                    f"{n_dated} dated; newest {newest[-80:]}")

    before, after = row["fingerprint_before"], row["fingerprint_after"]
    loaded = (reg["latest_period_loaded"] or "").strip()
    period = row["detected_period"]

    # The load gap is asked first, and the fingerprint second. They answer
    # different questions: the fingerprint says whether the publisher has
    # changed anything since the last look, the gap says whether what is
    # published is ahead of what is loaded.
    #
    # Fingerprint-first is wrong and was wrong here. Once an edition has been
    # detected the fingerprint matches on every later check, so a genuinely
    # pending edition reports no_change forever and disappears from the due
    # list without ever being loaded. That is the exact silent failure this
    # log exists to prevent.
    # Where the detector can name the edition, ask the database directly
    # whether that edition has been loaded. This is the same provenance test
    # the revision check uses, and it is exact where a period comparison
    # would be a category error.
    if det.get("edition_key"):
        edition = re.search(det["edition_key"], newest, re.I)
        edition = edition.group(1) if edition else None
        seen = edition_loaded(reg["target_table"], edition) if edition else None
        row["notes"] += f"; edition '{edition}'"
        if seen is True:
            row["outcome"] = "no_change"
            row["notes"] += " already present in the target table"
            return row
        if seen is False:
            row["outcome"] = "new_edition"
            row["notes"] += " not yet loaded"
            return row
        row.update(outcome="check_failed",
                   error_detail="the edition could not be read from the "
                                "newest link, or the target table records no "
                                "source, so whether this edition is loaded "
                                "is unestablished")
        return row

    # A period comparison is only valid when the detected period and the
    # loaded period mean the same thing. latest_period_loaded is always a
    # reference period, so anything else must not be compared against it —
    # doing so reports a new edition on every check, forever. Declared type
    # missing is a refusal, not a licence to guess.
    ptype = reg.get("detected_period_type")
    if period and loaded and ptype != "reference_period":
        row.update(outcome="check_failed",
                   error_detail=(
                       f"detected_period_type is "
                       f"{ptype or 'undeclared'}, so the detected period "
                       f"({period}) is not comparable with "
                       f"latest_period_loaded ({loaded}), which is always a "
                       f"reference period. Declare the type, or give this "
                       f"source an edition_key so the edition itself can be "
                       f"looked up in the target table."))
        return row

    if period and loaded and period[:7] > loaded[:7]:
        row["outcome"] = "new_edition"
    elif before and before == after:
        row["outcome"] = "no_change"
    elif before and before != after:
        # Publisher moved the file but the period is not ahead of what is
        # loaded — a re-cut of an edition already held.
        row["outcome"] = "url_changed"
    elif period and loaded:
        row["outcome"] = "no_change"
    else:
        row.update(
            outcome="check_failed",
            error_detail=(
                f"baseline check: the newest link was found and "
                f"fingerprinted (detected period "
                f"{row['detected_period'] or 'unparseable'}), but "
                f"latest_period_loaded is "
                f"{'null' if not loaded else repr(loaded)} so there was "
                f"nothing to compare it against. Whether this source is "
                f"current is unestablished. Populate latest_period_loaded "
                f"to turn this into a real comparison."))
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("codes", nargs="*", help="source codes; default is every "
                                             "tier A/B source that is due")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = get_conn()
    conn.autocommit = False
    # source_registry carries a writer-only trigger: it is a generated
    # table, and a direct edit either reverts on the next backfill or
    # persists until someone adds a declaration. This connection is a
    # declared writer.
    cur_flag = conn.cursor()
    cur_flag.execute("SET ucws.registry_writer = 'on'")
    cur_flag.close()
    cur = conn.cursor()

    if args.codes:
        cur.execute("""
            SELECT source_code, landing_page_url, latest_period_loaded,
                   last_seen_fingerprint, revises_back_series, target_table,
                   detected_period_type
            FROM source_registry WHERE source_code = ANY(%s)
            ORDER BY source_code
        """, (args.codes,))
    else:
        cur.execute("""
            SELECT r.source_code, r.landing_page_url, r.latest_period_loaded,
                   r.last_seen_fingerprint, r.revises_back_series,
                   r.target_table, r.detected_period_type
            FROM source_registry r
            JOIN vw_source_due d ON d.source_code = r.source_code
            WHERE r.refresh_tier IN ('A','B')
              AND d.due_status IN ('due','overdue','check_stale')
            ORDER BY r.source_code
        """)
    cols = [d[0] for d in cur.description]
    targets = [dict(zip(cols, r)) for r in cur.fetchall()]
    if not targets:
        print("no eligible sources")
        return 0

    results = []
    for reg in targets:
        row = check_one(reg["source_code"], reg)
        results.append(row)
        if not args.dry_run:
            cur.execute("""
                INSERT INTO source_check_log
                    (source_code, check_method, outcome, fingerprint_before,
                     fingerprint_after, detected_period, http_status,
                     error_detail, notes)
                VALUES (%(source_code)s, %(check_method)s, %(outcome)s,
                        %(fingerprint_before)s, %(fingerprint_after)s,
                        %(detected_period)s, %(http_status)s,
                        %(error_detail)s, %(notes)s)
            """, row)
            # last_check_at records that a check happened, whatever it found.
            # The fingerprint only advances on a check that actually saw one.
            cur.execute("""
                UPDATE source_registry
                   SET last_check_at = now(),
                       last_seen_fingerprint =
                           COALESCE(%s, last_seen_fingerprint)
                 WHERE source_code = %s
            """, (row["fingerprint_after"], row["source_code"]))

    if args.dry_run:
        conn.rollback()
    else:
        conn.commit()
    cur.close()
    conn.close()

    width = max(len(r["source_code"]) for r in results) + 2
    print(f"{'S#':<{width}}{'OUTCOME':<14}{'HTTP':<6}{'PERIOD':<12}FINGERPRINT")
    print("-" * 72)
    for r in results:
        print(f"{r['source_code']:<{width}}{r['outcome']:<14}"
              f"{str(r['http_status'] or '-'):<6}"
              f"{str(r['detected_period'] or '-'):<12}"
              f"{(r['fingerprint_after'] or '-')[:16]}")
    print()
    for r in results:
        if r["error_detail"]:
            print(f"  S{r['source_code']}: {r['error_detail']}")
    if args.dry_run:
        print("\nDRY RUN — nothing written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
