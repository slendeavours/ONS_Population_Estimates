"""S19 (DWP Stat-Xplore PIP claimants): pure functions and constants for the
move onto the loader standard, and the one-off month relabel migration.
Importing this module needs no Stat-Xplore key, database or network;
migrate_months works only on a cursor the caller passes in.

Months are stored as sortable 'yyyymm' keys (docs/RULES.md rule 8). The old
table held text labels such as 'Apr-26'; label_to_key and relabel_plan are
the one-off migration's mapping.

Blanks and zeros (docs/RULES.md rule 1): a value is the sum of its parts only
if every part is present (a part absent from the response counts as
missing), otherwise None. A returned 0 stays 0. Never `or 0`.
"""
import hashlib
import re

import editions_core as core

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


# --- one-off migration: month labels -> 'yyyymm' keys in the live table ---

_KEY_FORM = re.compile(r"[0-9]{6}")
_MEASURES = ("pip_total_claimants", "pip_enhanced_daily_living")


def _state(cur, table: str, month=None) -> dict:
    """Row count, distinct months, per-month totals and null counts, and the
    per-area values of `month` (if given) of the live table."""
    cur.execute(f"SELECT COUNT(*) FROM public.{table}")
    rows = cur.fetchone()[0]
    cur.execute(f"""SELECT month, COUNT(*),
                           SUM(pip_total_claimants),
                           SUM(pip_enhanced_daily_living),
                           COUNT(*) - COUNT(pip_total_claimants),
                           COUNT(*) - COUNT(pip_enhanced_daily_living)
                    FROM public.{table} GROUP BY month ORDER BY month""")
    per_month = {r[0]: {"rows": r[1], "sum_total": r[2], "sum_enhanced": r[3],
                        "null_total": r[4], "null_enhanced": r[5]}
                 for r in cur.fetchall()}
    latest = None
    if month is not None:
        cur.execute(f"""SELECT lad24cd, pip_total_claimants,
                               pip_enhanced_daily_living
                        FROM public.{table} WHERE month = %s
                        ORDER BY lad24cd""", (month,))
        latest = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    return {"rows": rows, "months": sorted(per_month), "per_month": per_month,
            "latest": latest}


def _compare_states(before: dict, after: dict, plan: dict) -> list:
    """Differences between the before and after states, the months compared
    through the plan {label: key}; [] if none."""
    probs = []
    if before["rows"] != after["rows"]:
        probs.append(f"row count {before['rows']} -> {after['rows']}")
    if len(before["months"]) != len(after["months"]):
        probs.append(f"distinct months {len(before['months'])} -> "
                     f"{len(after['months'])}")
    for label, figs in before["per_month"].items():
        got = after["per_month"].get(plan[label])
        if got != figs:
            probs.append(f"{label} -> {plan[label]}: totals {figs} -> {got}")
    if before["latest"] != after["latest"]:
        probs.append("per-area values of the latest month differ")
    return probs


def migrate_months(cur, live_table: str = "la_pip_claimants", *,
                   commit: bool = False) -> dict:
    """Relabel the live table's months from labels ('Apr-26') to sortable
    'yyyymm' keys (docs/RULES.md rule 8), one UPDATE per label, touching only
    the month column.

    Runs inside the caller's transaction and never commits or rolls it back;
    `commit` only labels the report. The writes are made in a savepoint of
    their own: if a check fails the savepoint is rolled back and the function
    halts (SystemExit), leaving the caller's transaction as it was.

    Halts, changing nothing, on: an empty table, a mix of label and key
    forms, an invalid or ambiguous label. Returns {"status": "already
    migrated"} if every month is already a key. Otherwise checks that rows,
    distinct months, per-month totals and null counts, and the per-area
    values of the month the relabelled MAX(month) selects are identical
    before and after, and returns the report."""
    table = core._ident(live_table)
    cur.execute(f"SELECT DISTINCT month FROM public.{table} ORDER BY month")
    held = [r[0] for r in cur.fetchall()]
    if not held:
        core.halt(f"{table} holds no rows; nothing to migrate")
    keys = [h for h in held if _KEY_FORM.fullmatch(h)]
    if len(keys) == len(held):
        return {"status": "already migrated"}
    if keys:
        core.halt(f"{table} holds a mix of month forms: keys {keys} and "
                  f"labels {[h for h in held if h not in keys]}; refusing "
                  "to migrate")
    try:
        plan = relabel_plan(held)
    except ValueError as e:
        core.halt(f"{table}: {e}; nothing changed")
    latest_key = max(plan.values())
    latest_label = next(lb for lb, k in plan.items() if k == latest_key)

    before = _state(cur, table, latest_label)
    with core._own_savepoint(cur, "s19_migrate_months"):
        for label, key in plan.items():
            cur.execute(f"UPDATE public.{table} SET month = %s "
                        "WHERE month = %s", (key, label))
        cur.execute(f"SELECT MAX(month) FROM public.{table}")
        selected = cur.fetchone()[0]
        after = _state(cur, table, selected)
        probs = _compare_states(before, after, plan)
        if selected != latest_key:
            probs.append(f"MAX(month) selects {selected}, expected "
                         f"{latest_key}")
        if probs:
            core.halt(f"{table} month relabel failed its checks, rolled "
                      "back: " + "; ".join(probs))
    return {"status": "migrated", "table": table, "committed": commit,
            "mapping": dict(plan), "reverse": {k: lb for lb, k in plan.items()},
            "latest_label": latest_label, "latest_key": latest_key,
            "before": before, "after": after}
