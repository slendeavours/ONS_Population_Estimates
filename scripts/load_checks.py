"""Shared gates every load runs (RULES.md sections 1, 3, 4).

Each check returns a list of problems (empty means pass), except blank_report,
which returns a dict. Nothing here halts or writes; the caller decides what a
problem means. Extracted from the per-source loaders (s1_editions,
s1b_editions, ro4_editions), which are unchanged by this module.
"""
from typing import Iterable

from editions_core import EditionSpec, chain_tip


def check_coverage(cur, spec: EditionSpec, period: str, edition: int,
                   expected_n: int) -> list:
    """The stored edition exists and holds expected_n rows, each with a
    distinct key (a duplicate key is a problem even when the count is right).

    Extracted from s1b_editions.check_coverage (rows = authorities x
    categories) and ro4_editions.check_coverage (rows = authorities); both
    required the exact row count, so expected_n is the caller's derived count
    for the whole edition. Where those also compared a distinct-authority
    count, the distinct-key test here is the general form of it.
    """
    keys = ", ".join(spec.key_cols)
    cur.execute(f"""SELECT COUNT(*), COUNT(DISTINCT ({keys}))
                    FROM public.{spec.editions_table}
                    WHERE {spec.period_col} = %s AND edition = %s""",
                (period, edition))
    rows, distinct = cur.fetchone()
    tag = f"{period} ed{edition}"
    if rows == 0:
        return [f"{tag}: no such edition, expected {expected_n} rows"]
    bad = []
    if rows != expected_n:
        bad.append(f"{tag}: {rows}/{expected_n} rows (found/expected)")
    if distinct != rows:
        bad.append(f"{tag}: {rows - distinct} duplicate key row(s)")
    return bad


def check_codes(cur, codes: Iterable[str]) -> list:
    """Resolve each code through la_code_lookup (old_code -> new_code) FIRST,
    then orphan-check the resolved code against la_boundaries. A code that is
    still not in la_boundaries is reported as 'UNEXPLAINED <code>'; there is
    no 'expected' or 'harmless' category.

    Extracted from the la_code_lookup handling in s1_editions.check_registry,
    which only tested membership of the lookup (old or new) and so passed a
    code whose new_code was itself unknown to la_boundaries; this is stricter.
    """
    codes = sorted({c for c in codes})
    if not codes:
        return []
    cur.execute("SELECT old_code, new_code FROM public.la_code_lookup "
                "WHERE old_code = ANY(%s)", (codes,))
    mapped = {}
    for old, new in cur.fetchall():
        mapped.setdefault(old, set()).add(new)
    every = sorted(set(codes) | {n for s in mapped.values() for n in s})
    cur.execute("SELECT lad24cd FROM public.la_boundaries "
                "WHERE lad24cd = ANY(%s)", (every,))
    known = {r[0] for r in cur.fetchall()}
    return _unexplained(codes, mapped, known)


def _unexplained(codes, mapped: dict, known: set) -> list:
    """Pure decision for check_codes. A code is resolved if it is itself in
    known (la_boundaries) or any of its lookup targets (mapped[code]) is.
    One-to-many splits are deferred to la_succession: a code mapped to two
    targets passes when either is known."""
    return [f"UNEXPLAINED {c}" for c in sorted(set(codes))
            if not ((mapped.get(c, set()) | {c}) & known)]


def check_identity(file_facts: dict, entry: dict, keys: tuple) -> list:
    """For each key, what the file says about itself must equal the manifest
    entry's value. A key missing from file_facts is a mismatch. The
    extractors stay in the source loaders (cf. s1_editions.check_cover,
    ro4_editions.check_front_page)."""
    bad = []
    for k in keys:
        want = entry.get(k)
        if k not in file_facts:
            bad.append(f"{k}: file gives no value, manifest has {want!r}")
        elif file_facts[k] != want:
            bad.append(f"{k}: file says {file_facts[k]!r}, manifest has "
                       f"{want!r}")
    return bad


def check_latest_equals_live(cur, spec: EditionSpec, periods=None) -> list:
    """For each live period (or the given ones) the latest edition equals the
    live rows, NULL-safe (0 is not NULL), on every data column of the spec.

    ro4_editions compared only key + refresh columns; s1b_editions compared
    every data column (key, period, value and extra). This takes the s1b
    variant, the stricter one. Never halts: a missing or branched chain is a
    problem string.
    """
    cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                f"public.{spec.live_table}")
    live_periods = sorted(r[0] for r in cur.fetchall())
    cols = ", ".join(spec.data_cols)
    bad = []
    for p in (periods or live_periods):
        try:
            ed = chain_tip(cur, spec, p)
        except (LookupError, ValueError) as e:
            bad.append(f"{p}: {e}")
            continue
        a = (f"SELECT {cols} FROM public.{spec.editions_table} "
             f"WHERE {spec.period_col} = %s AND edition = %s")
        b = (f"SELECT {cols} FROM public.{spec.live_table} "
             f"WHERE {spec.period_col} = %s")
        counts = []
        for x, xa, y, ya in ((a, (p, ed), b, (p,)), (b, (p,), a, (p, ed))):
            cur.execute(f"SELECT COUNT(*) FROM (({x}) EXCEPT ({y})) q",
                        xa + ya)
            counts.append(cur.fetchone()[0])
        if any(counts):
            bad.append(f"{p} ed{ed}: {counts[0]} edition-only, {counts[1]} "
                       "live-only rows")
    return bad


def blank_report(rows: list, cols: Iterable[str],
                 flag_col: "str | None" = None) -> dict:
    """Per column: how many cells are NULL, how many are 0, and how many rows
    carry a non-null flag in flag_col."""
    flagged = (sum(1 for r in rows if r.get(flag_col) is not None)
               if flag_col else 0)
    return {c: {"null": sum(1 for r in rows if r.get(c) is None),
                "zero": sum(1 for r in rows
                            if r.get(c) is not None and r[c] == 0),
                "flagged": flagged}
            for c in cols}


def blank_regression(new_rows: list, old_rows: list, key_cols: tuple,
                     cols: Iterable[str]) -> list:
    """Cells that were NULL before and are 0 now, or 0 before and NULL now,
    compared by key. Unchanged cells and keys new in new_rows are ignored;
    NULL is never treated as 0."""
    def key(r):
        return tuple(r[k] for k in key_cols)
    old = {key(r): r for r in old_rows}
    bad = []
    for r in new_rows:
        o = old.get(key(r))
        if o is None:
            continue
        for c in cols:
            was, now = o.get(c), r.get(c)
            if was is None and now is not None and now == 0:
                bad.append(f"{key(r)} {c}: NULL before, 0 now")
            elif was is not None and was == 0 and now is None:
                bad.append(f"{key(r)} {c}: 0 before, NULL now")
    return bad
