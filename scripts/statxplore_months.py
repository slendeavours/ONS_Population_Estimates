"""Generic Stat-Xplore helpers shared by the S8b and S19 loaders: cube
parsing and yyyymm month planning. No database, no network."""
import re

MONTH_RE = re.compile(r"(19|20)[0-9]{2}(0[1-9]|1[0-2])")


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


def month_from_member(member_id):
    """Last colon segment as yyyymm (six digits), else None."""
    last = str(member_id).split(":")[-1]
    return last if MONTH_RE.fullmatch(last) else None


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
