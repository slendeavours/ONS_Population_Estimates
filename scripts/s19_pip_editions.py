"""S19 (DWP Stat-Xplore PIP claimants): pure functions and constants for the
move onto the loader standard. No I/O: importing this module needs no
Stat-Xplore key, database or network.

Months are stored as sortable 'yyyymm' keys (docs/RULES.md rule 8). The old
table held text labels such as 'Apr-26'; label_to_key and relabel_plan are
the one-off migration's mapping.

Blanks and zeros (docs/RULES.md rule 1): a value is the sum of its parts only
if every part is present (a part absent from the response counts as
missing), otherwise None. A returned 0 stays 0. Never `or 0`.
"""
import hashlib
import re

LABEL_FORMAT = "%b-%y"

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_LABEL_RE = re.compile(r"([A-Z][a-z]{2})-([0-9]{2})")
_KEY_RE = re.compile(r"([0-9]{4})(0[1-9]|1[0-2])")


def label_to_key(label: str) -> str:
    """'Apr-26' -> '202604'. Only a capitalised English three-letter month,
    a hyphen and a two-digit year (20yy); anything else is a ValueError."""
    m = _LABEL_RE.fullmatch(label) if isinstance(label, str) else None
    if not m or m.group(1) not in _MONTHS:
        raise ValueError(f"not a month label like 'Apr-26': {label!r}")
    return f"20{m.group(2)}{_MONTHS.index(m.group(1)) + 1:02d}"


def key_to_label(key: str) -> str:
    """'202604' -> 'Apr-26' (a display label; never stored as the key)."""
    m = _KEY_RE.fullmatch(key) if isinstance(key, str) else None
    if not m or not m.group(1).startswith("20"):
        raise ValueError(f"not a yyyymm month key: {key!r}")
    return f"{_MONTHS[int(m.group(2)) - 1]}-{m.group(1)[2:]}"


def relabel_plan(held_labels: list) -> dict:
    """{label: key} for each distinct held label. ValueError if a label is
    invalid or two distinct labels map to the same key."""
    plan, owner = {}, {}
    for label in dict.fromkeys(held_labels):
        key = label_to_key(label)
        if key in owner:
            raise ValueError(f"labels {owner[key]!r} and {label!r} both map "
                             f"to {key}")
        owner[key] = label
        plan[label] = key
    return plan


def _sum_parts(raw: dict, codes: list):
    parts = [raw.get(c) for c in codes]
    if parts and all(p is not None for p in parts):
        return sum(parts)
    return None


def build_records(total_raw: dict, enhanced_raw: dict, lad_to_uris: dict,
                  month: str) -> list:
    """One record per canonical lad24cd, sorted; each measure independently
    the sum of its parts only if every part is present, else None. The
    geography code of a URI is its last ':' segment."""
    records = []
    for lad, uris in sorted(lad_to_uris.items()):
        codes = [u.split(":")[-1] for u in uris]
        records.append({"lad24cd": lad, "month": month,
                        "pip_total_claimants": _sum_parts(total_raw, codes),
                        "pip_enhanced_daily_living":
                            _sum_parts(enhanced_raw, codes)})
    return records


def _cell(r: dict, col: str) -> str:
    v = r[col]
    if v is None:
        return "NULL"
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError(f"{col} must be an int or None, got {v!r} for "
                         f"{r['lad24cd']}")
    return str(v)


def content_sha256(records: list) -> str:
    """SHA-256 of the records sorted by (lad24cd, month); each line
    lad24cd|month|total-or-NULL|enhanced-or-NULL."""
    rows = sorted(records, key=lambda r: (r["lad24cd"], r["month"]))
    lines = ["|".join([r["lad24cd"], r["month"],
                       _cell(r, "pip_total_claimants"),
                       _cell(r, "pip_enhanced_daily_living")]) for r in rows]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
