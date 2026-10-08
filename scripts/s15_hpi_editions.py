"""S15 - Land Registry UK HPI: pure functions and constants.

No I/O, no network, no database, no environment requirements: importing this
module needs no key and no connection. The loader that uses it lives
elsewhere. Logic ported from s15_hpi_build.py with identical behaviour.
"""
import hashlib
import math
import re
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

ENGLISH_LA_PREFIXES = ("E06", "E07", "E08", "E09")
MIN_PERIOD = date(2022, 1, 1)

HARD_RECODES = {
    "E08000038": "E08000016",  # Barnsley
    "E08000039": "E08000019",  # Sheffield
}

BASE_URL = "https://www.gov.uk"

# Column scales: prices numeric(12,2), annual change numeric(6,2).
_PRICE_Q = Decimal("0.01")
_CHANGE_Q = Decimal("0.01")
VALUE_COLUMNS = ("avg_price_all", "avg_price_all_sa", "annual_change_pct",
                 "avg_price_detached", "avg_price_semi",
                 "avg_price_terraced", "avg_price_flat")

EDITION_RE = re.compile(r"^Average-prices-(?:Property-Type-)?(\d{4}-\d{2})\.csv$")


def edition_from_filename(name: str) -> str:
    """'Average-prices-2026-07.csv' or 'Average-prices-Property-Type-2026-07.csv'
    -> '2026-07'. Anything else raises ValueError naming the input."""
    m = EDITION_RE.match(name) if isinstance(name, str) else None
    if not m:
        raise ValueError(f"not a recognised HPI file name: {name!r}")
    return m.group(1)


def safe_numeric(val):
    if val is None or val == "" or val == "NA":
        return None
    try:
        v = float(val)
        return None if math.isnan(v) or math.isinf(v) else v
    except (ValueError, TypeError):
        return None


def reconcile(area_code, *, valid_lads, code_lookup, hard_recodes):
    """Hard recodes first, then the code itself if valid, then the lookup."""
    if area_code in hard_recodes:
        return hard_recodes[area_code]
    if area_code in valid_lads:
        return area_code
    if area_code in code_lookup:
        return code_lookup[area_code]
    return None


def _dec(val, quantum):
    """CSV text -> Decimal at the column scale, or None (never 0 for blanks)."""
    v = safe_numeric(val)
    if v is None:
        return None
    return Decimal(repr(v)).quantize(quantum, rounding=ROUND_HALF_UP)


def build_records(avg_rows, pt_rows, *, valid_lads, code_lookup,
                  hard_recodes=HARD_RECODES, min_period=MIN_PERIOD):
    """Join the two CSVs on (Area_Code, Date), keep English areas from
    min_period on, reconcile codes. Returns (records_by_period, unresolved,
    no_property_type_count)."""
    pt_lookup = {(r["Area_Code"], r["Date"]): r for r in pt_rows}
    by_period = {}
    unresolved = {}
    no_pt = 0
    for r in avg_rows:
        area_code = r["Area_Code"]
        if not area_code.startswith(ENGLISH_LA_PREFIXES):
            continue
        period = date.fromisoformat(r["Date"])
        if period < min_period:
            continue
        lad = reconcile(area_code, valid_lads=valid_lads,
                        code_lookup=code_lookup, hard_recodes=hard_recodes)
        if lad is None:
            unresolved[area_code] = unresolved.get(area_code, 0) + 1
            continue
        pt = pt_lookup.get((area_code, r["Date"]))
        if pt is None:
            no_pt += 1
        iso = period.isoformat()
        by_period.setdefault(iso, []).append({
            "lad24cd": lad,
            "period": iso,
            "avg_price_all": _dec(r.get("Average_Price"), _PRICE_Q),
            "avg_price_all_sa": _dec(r.get("Average_Price_SA"), _PRICE_Q),
            "annual_change_pct": _dec(r.get("Annual_Change"), _CHANGE_Q),
            "avg_price_detached": _dec(pt["Detached_Average_Price"], _PRICE_Q) if pt else None,
            "avg_price_semi": _dec(pt["Semi_Detached_Average_Price"], _PRICE_Q) if pt else None,
            "avg_price_terraced": _dec(pt["Terraced_Average_Price"], _PRICE_Q) if pt else None,
            "avg_price_flat": _dec(pt["Flat_Average_Price"], _PRICE_Q) if pt else None,
        })
    for recs in by_period.values():
        recs.sort(key=lambda x: x["lad24cd"])
    return by_period, unresolved, no_pt


def file_period_range(records_by_period):
    if not records_by_period:
        raise ValueError("no periods in the data")
    keys = sorted(records_by_period)
    return keys[0], keys[-1]


def check_identity(name1, name2, records_by_period):
    """Problems (empty list = fine). Raises ValueError for an unrecognisable
    file name."""
    e1, e2 = edition_from_filename(name1), edition_from_filename(name2)
    problems = []
    if e1 != e2:
        problems.append(f"file editions differ: {name1} is {e1}, {name2} is {e2}")
    if not records_by_period:
        problems.append("no periods in the data")
    else:
        latest = max(records_by_period)[:7]
        if latest != e1:
            problems.append(f"edition {e1} is not the latest period in the "
                            f"data (latest is {latest})")
    return problems


def _cell(r, col):
    v = r[col]
    if v is None:
        return "NULL"
    if isinstance(v, bool) or not isinstance(v, Decimal):
        raise ValueError(f"{col} must be a Decimal or None, got {v!r} for "
                         f"{r['lad24cd']}")
    return format(v.quantize(_PRICE_Q, rounding=ROUND_HALF_UP), "f")


def content_sha256(records) -> str:
    """SHA-256 of one period's records sorted by lad24cd; NULL is distinct
    from 0."""
    rows = sorted(records, key=lambda r: (r["lad24cd"], r["period"]))
    lines = ["|".join([r["lad24cd"], r["period"]]
                      + [_cell(r, c) for c in VALUE_COLUMNS]) for r in rows]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


_DL_PAGE_RE = re.compile(
    r"""href=["']((?:https://www\.gov\.uk)?/government/statistical-data-sets/"""
    r"""uk-house-price-index-data-downloads-[^"'#?\s]*)["']""")
_AVG_RE = re.compile(r"""href=["']([^"']*?/Average-prices-(\d{4}-\d{2})\.csv[^"']*)["']""")
_PT_RE = re.compile(
    r"""href=["']([^"']*?/Average-prices-Property-Type-(\d{4}-\d{2})\.csv[^"']*)["']""")


def _absolute(href):
    return BASE_URL + href if href.startswith("/") else href


def find_download_page(html: str) -> str:
    m = _DL_PAGE_RE.search(html)
    if not m:
        raise ValueError("no uk-house-price-index-data-downloads link found; "
                         f"page excerpt: {html[:300]!r}")
    return _absolute(m.group(1))


def find_csv_links(html: str):
    """(average prices URL, property type URL, 'YYYY-MM'). Raises ValueError
    if either file is missing or the two editions differ."""
    a, p = _AVG_RE.search(html), _PT_RE.search(html)
    if not a or not p:
        raise ValueError("expected both Average-prices-YYYY-MM.csv and "
                         "Average-prices-Property-Type-YYYY-MM.csv links; "
                         f"found avg={bool(a)}, property type={bool(p)}; "
                         f"page excerpt: {html[:300]!r}")
    if a.group(2) != p.group(2):
        raise ValueError(f"file editions differ: average prices {a.group(2)}, "
                         f"property type {p.group(2)}")
    return _absolute(a.group(1)), _absolute(p.group(1)), a.group(2)
