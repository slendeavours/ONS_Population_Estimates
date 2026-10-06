"""Check the national TA year-on-year in staging_national is like-for-like.

The figure is recomputed in Python from the rows of la_statutory_homelessness,
a different route from the SQL in the W1 National Aggregates node, and compared
with the latest complete run. Like-for-like means authorities with
households_in_ta > 0 in BOTH the latest period and the same period a year
earlier. Dividing the all-reporting current total by the all-reporting prior
total mixes two different sets of authorities: run 22 reported +13.29% where
the matched set gives about +3.14%.

Usage:
    python scripts/verify_national_ta.py        # exit 0 if the latest run agrees
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _db import get_readonly_conn  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def recompute(cur):
    cur.execute("SELECT MAX(period) FROM la_statutory_homelessness")
    cur_p = cur.fetchone()[0]
    prev_p = f"{int(cur_p[:4]) - 1}{cur_p[4:]}"
    cur.execute("SELECT period, lad24cd, households_in_ta "
                "FROM la_statutory_homelessness WHERE period IN (%s, %s)",
                (cur_p, prev_p))
    now, before = {}, {}
    for period, code, ta in cur.fetchall():
        if ta is not None and ta > 0:
            (now if period == cur_p else before)[code] = ta
    both = sorted(set(now) & set(before))
    cur_m = sum(now[c] for c in both)
    prev_m = sum(before[c] for c in both)
    return {"period": cur_p, "prior_period": prev_p,
            "current_all": sum(now.values()), "prior_all": sum(before.values()),
            "matched": len(both), "current_matched": cur_m,
            "prior_matched": prev_m,
            "yoy": round((cur_m - prev_m) * 100.0 / prev_m, 2) if prev_m else None}


def main():
    conn = get_readonly_conn()
    cur = conn.cursor()
    want = recompute(cur)
    cur.execute("""
        SELECT run_id, ta_households_current, ta_households_prev_year, ta_yoy_pct,
               ta_matched_authorities, ta_households_current_matched,
               ta_households_prev_year_matched
        FROM staging_national
        WHERE run_id = (SELECT MAX(run_id) FROM staging_runs WHERE status = 'complete')
        """)
    row = cur.fetchone()
    print(f"recomputed ({want['period']} vs {want['prior_period']}): {want}")
    if row is None:
        print("FAIL: no complete run in staging_national")
        return 1
    run_id, cur_all, prior_all, yoy, n, cur_m, prev_m = row
    print(f"run {run_id}: current {cur_all}, prior {prior_all}, yoy {yoy}, "
          f"matched {n}, current_matched {cur_m}, prior_matched {prev_m}")
    checks = [
        ("ta_households_current (all reporting)", cur_all, want["current_all"]),
        ("ta_households_prev_year (all reporting)", prior_all, want["prior_all"]),
        ("ta_matched_authorities", n, want["matched"]),
        ("ta_households_current_matched", cur_m, want["current_matched"]),
        ("ta_households_prev_year_matched", prev_m, want["prior_matched"]),
        ("ta_yoy_pct", float(yoy) if yoy is not None else None, want["yoy"]),
    ]
    bad = [(k, got, exp) for k, got, exp in checks if got != exp]
    for k, got, exp in checks:
        print(f"  {'PASS' if got == exp else 'FAIL'} {k}: stored {got}, recomputed {exp}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
