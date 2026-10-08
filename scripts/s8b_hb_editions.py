"""S8b (DWP Stat-Xplore Housing Benefit caseload by accommodation type):
pure functions and constants. No I/O, no database, no network, no API key
needed at import.

Blanks and zeros (docs/RULES.md rule 1): an API null is None, a returned 0 is
0, and a blank is never coerced to zero. A merged-area total is the sum of its
parts only if every part is present; otherwise it is None.
"""
import hashlib
import re

ACCOM_TYPES = ("SA", "TA", "OTHER", "UNKNOWN")

# Member ids as in the old loader (s8b_hb_accom_type_build.ACCOM_MEMBERS).
ACCOM_MEMBER_IDS = {
    "SA": "str:value:hb_new:V_F_HB_NEW:SATA:C_SATA:1",
    "TA": "str:value:hb_new:V_F_HB_NEW:SATA:C_SATA:2",
    "OTHER": "str:value:hb_new:V_F_HB_NEW:SATA:C_SATA:9",
    "UNKNOWN": "str:value:hb_new:V_F_HB_NEW:SATA:C_SATA:99",
}

_MONTH_RE = re.compile(r"(19|20)[0-9]{2}(0[1-9]|1[0-2])")


def parse_cube(resp: dict) -> dict:
    """Map source geography code to value (int or None) from a cube response."""
    cubes = resp["cubes"]
    values = cubes[list(cubes.keys())[0]]["values"]
    items = resp["fields"][0]["items"]
    if len(values) != len(items):
        raise ValueError(
            f"Cube has {len(values)} values but {len(items)} geography items")
    out = {}
    for i, item in enumerate(items):
        code = item["uris"][0].split(":")[-1]
        v = values[i]
        while isinstance(v, list):
            if not v:
                raise ValueError(f"Empty value list for geography {code}")
            v = v[0]
        if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
            raise ValueError(
                f"Non-integer value {v!r} for geography {code}; refusing to coerce")
        out[code] = v
    return out


def build_records(raw, lad_to_uris, month, accom_type):
    """One record per canonical lad24cd; total is None unless every part is present."""
    records = []
    for lad, uris in sorted(lad_to_uris.items()):
        parts = [raw.get(u.split(":")[-1]) for u in uris]
        if parts and all(p is not None for p in parts):
            total = sum(parts)
        else:
            total = None
        records.append({"lad24cd": lad, "month": month,
                        "accom_type": accom_type, "claimants": total})
    return records


def month_from_member(member_id):
    """Last colon segment as yyyymm (six digits), else None."""
    last = str(member_id).split(":")[-1]
    return last if _MONTH_RE.fullmatch(last) else None


def available_months(date_members):
    months = {month_from_member(d.get("id", "")) for d in date_members}
    months.discard(None)
    return sorted(months)


def plan_months(held, available, recheck_n=6, recheck_all=False):
    """Return (new, recheck), both ascending and never overlapping.

    new: every available month that is not held and is later than the
    EARLIEST held month (gaps inside the held range are loaded; history before
    the first held month is never auto-loaded). If nothing is held, new is
    empty: the loader must require an explicit --months list.
    recheck: the latest recheck_n held months that are still available (all of
    them when recheck_all).
    """
    avail = sorted(set(available))
    earliest = min(held) if held else None
    new = [a for a in avail if earliest is not None and a > earliest and a not in held]
    still = [a for a in avail if a in held]
    if recheck_all:
        recheck = still
    elif recheck_n > 0:
        recheck = still[-recheck_n:]
    else:
        recheck = []
    return new, recheck


def content_sha256(records):
    """SHA-256 of the records; meant for one month per call (month is still
    part of the sort key and the serialisation, so mixed input stays stable)."""
    rows = sorted(records, key=lambda r: (r["lad24cd"], r["accom_type"], r["month"]))
    lines = []
    for r in rows:
        c = r["claimants"]
        if c is not None and (isinstance(c, bool) or not isinstance(c, int)):
            raise ValueError(
                f"claimants must be an int or None, got {c!r} for {r['lad24cd']}")
        lines.append("|".join([r["lad24cd"], r["accom_type"], r["month"],
                               "NULL" if c is None else str(c)]))
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
