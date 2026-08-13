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
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _db import get_conn  # noqa: E402

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
    "18": dict(
        method="landing_page",
        pattern=r'href="([^"]*\.xlsx)"',
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
    "9b": dict(
        method="landing_page",
        pattern=r'href="([^"]*/performance-[a-z]+-\d{4}[^"]*)"',
        note="performance-{month}-{year} publication pages; "
             "docs/nodes/s9b_node1_fetch_mhsds_monthly.md",
    ),
}


MONTH_RE = "|".join(MONTHS)


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
    if loaded_ym and row["detected_period"]:
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
    if not reg["landing_page_url"]:
        row.update(outcome="check_failed",
                   error_detail="no landing_page_url in the registry")
        return row

    if det.get("govuk_query"):
        return check_govuk(row, det, reg)

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
    cur = conn.cursor()

    if args.codes:
        cur.execute("""
            SELECT source_code, landing_page_url, latest_period_loaded,
                   last_seen_fingerprint, revises_back_series, target_table
            FROM source_registry WHERE source_code = ANY(%s)
            ORDER BY source_code
        """, (args.codes,))
    else:
        cur.execute("""
            SELECT r.source_code, r.landing_page_url, r.latest_period_loaded,
                   r.last_seen_fingerprint, r.revises_back_series,
                   r.target_table
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
