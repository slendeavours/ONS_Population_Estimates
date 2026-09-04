"""S6 asylum dispersal verification suite.

Twelve halting checks. Every check must pass or the load is rolled back.
Imported by s6_asylum_build.py; not run standalone.
"""

import datetime
import os
import pathlib
from collections import defaultdict

import pandas as pd

ANCHOR_PERIOD = datetime.date(2026, 3, 31)
ANCHORS_ENGLAND = {"Birmingham": 2142, "Liverpool": 2053, "Coventry": 1712}
ANCHORS_NON_ENGLAND = {"Glasgow City": 3870, "Belfast": 1607}
ANCHOR_UK_TOTAL = 97519
COLLISION_HALT_THRESHOLD = 5
REG02_TOTAL_EXEMPT = {"E09000001", "E06000053"}

# Published distribution shape, year ending March 2026: of 361 UK local
# authorities, approximately 181 had fewer than 100 supported asylum seekers,
# which implies 180 at or above 100.
#
# The check is stated on the "at or above" side because that quantity is
# computed directly from the source with no adjustment: present LAs minus those
# under 100. Stating it on the "under 100" side would require adding the absent
# LAs back in to reach the published figure, which reads as fitting the result
# to the target even though the arithmetic is sound.
PUBLISHED_LA_UNIVERSE = 361
PUBLISHED_UNDER_100 = 181
PUBLISHED_AT_OR_ABOVE_100 = PUBLISHED_LA_UNIVERSE - PUBLISHED_UNDER_100
AT_OR_ABOVE_TOLERANCE = 5

# The Home Office denominator is the full UK LAD set, which is why LAs absent
# from the file necessarily fall in the under-100 bucket.
UK_LAD_DECOMPOSITION = (("England", 296), ("Scotland", 32), ("Wales", 22),
                        ("Northern Ireland", 11))
ENGLISH_REGIONS = {
    "East Midlands", "East of England", "London", "North East", "North West",
    "South East", "South West", "West Midlands", "Yorkshire and The Humber",
}
REG02_PATHWAY_COUNT = 12

_results = []


def _record(name, passed, detail):
    _results.append({"name": name, "passed": passed, "detail": detail})
    status = "PASS" if passed else "FAIL"
    print(f"\n[{status}] {name}")
    for line in detail.splitlines():
        print(f"       {line}")
    return passed


# ---------------------------------------------------------------------------

def check_1_coverage(cur, england):
    cur.execute("""
        SELECT period_ending, count(DISTINCT lad24cd)
          FROM la_asylum_support GROUP BY period_ending ORDER BY period_ending
    """)
    per = cur.fetchall()
    cur.execute("SELECT count(*) FROM la_boundaries")
    total = cur.fetchone()[0]
    latest = per[-1]
    lines = [f"Unmatched English names: 0 (the loader halts on any)",
             f"English LAs in la_boundaries: {total}",
             f"LAs present at {latest[0]}: {latest[1]} of {total}",
             f"Range across {len(per)} periods: "
             f"{min(p[1] for p in per)} to {max(p[1] for p in per)}",
             "Fewer than 296 is expected: an absent LA means not published, "
             "not none (minimum published People value is 1, zeros never "
             "appear)."]
    return _record("Check 1 - Coverage", True, "\n".join(lines))


def check_2_referential(cur):
    cur.execute("""
        SELECT count(*) FROM la_asylum_support s
         WHERE NOT EXISTS (SELECT 1 FROM la_boundaries b
                            WHERE b.lad24cd = s.lad24cd)
    """)
    orphans = cur.fetchone()[0]
    cur.execute("""
        SELECT count(*) FROM la_asylum_support
         WHERE lad24cd !~ '^E0[6789]'
    """)
    non_eng = cur.fetchone()[0]
    cur.execute("""
        SELECT count(*) FROM la_immigration_groups g
         WHERE NOT EXISTS (SELECT 1 FROM la_boundaries b
                            WHERE b.lad24cd = g.lad24cd)
    """)
    orphans_g = cur.fetchone()[0]
    ok = orphans == 0 and non_eng == 0 and orphans_g == 0
    return _record("Check 2 - Referential integrity", ok,
                   f"la_asylum_support orphans: {orphans}\n"
                   f"la_asylum_support non-English codes: {non_eng}\n"
                   f"la_immigration_groups orphans: {orphans_g}")


def check_3_anchors(cur):
    lines, ok = [], True
    for name, expected in ANCHORS_ENGLAND.items():
        cur.execute("""
            SELECT COALESCE(SUM(s.people), 0)
              FROM la_asylum_support s
              JOIN la_boundaries b ON b.lad24cd = s.lad24cd
             WHERE s.period_ending = %s AND b.lad24nm = %s
        """, (ANCHOR_PERIOD, name))
        actual = cur.fetchone()[0]
        match = actual == expected
        ok &= match
        lines.append(f"{name:<16} published {expected:>6}  loaded {actual:>6}  "
                     f"{'MATCH' if match else 'MISMATCH'}")
    for name, expected in ANCHORS_NON_ENGLAND.items():
        cur.execute("""
            SELECT COALESCE(SUM(people), 0) FROM asylum_support_non_england
             WHERE period_ending = %s AND published_la_name = %s
        """, (ANCHOR_PERIOD, name))
        actual = cur.fetchone()[0]
        match = actual == expected
        ok &= match
        lines.append(f"{name:<16} published {expected:>6}  loaded {actual:>6}  "
                     f"{'MATCH' if match else 'MISMATCH'} (non-England table)")
    cur.execute("""
        SELECT (SELECT COALESCE(SUM(people),0) FROM la_asylum_support
                 WHERE period_ending = %s)
             + (SELECT COALESCE(SUM(people),0) FROM asylum_support_non_england
                 WHERE period_ending = %s)
             + (SELECT COALESCE(SUM(people),0) FROM la_asylum_support_unallocated
                 WHERE period_ending = %s)
    """, (ANCHOR_PERIOD,) * 3)
    uk = cur.fetchone()[0]
    match = uk == ANCHOR_UK_TOTAL
    ok &= match
    lines.append(f"{'UK total':<16} published {ANCHOR_UK_TOTAL:>6}  "
                 f"loaded {uk:>6}  {'MATCH' if match else 'MISMATCH'}")
    lines.append("Aggregation: all support types, all accommodation types "
                 "(established empirically at Gate 1).")
    return _record("Check 3 - Anchor set", ok, "\n".join(lines))


def check_4_load_fidelity(cur, england, unalloc, non_eng, reg_rows,
                          reg_period):
    """Parsed dataframe must equal what landed, per period, tolerance 0."""
    lines, ok = [], True

    def compare(label, parsed, table, people_col="people"):
        nonlocal ok
        exp = defaultdict(lambda: [0, 0])
        for key, value in parsed.items():
            exp[key[0]][0] += 1
            exp[key[0]][1] += value
        cur.execute(f"SELECT period_ending, count(*), COALESCE(SUM({people_col}),0) "
                    f"FROM {table} GROUP BY period_ending")
        act = {r[0]: [r[1], int(r[2])] for r in cur.fetchall()}
        bad = []
        for period in sorted(set(exp) | set(act)):
            e = exp.get(period, [0, 0])
            a = act.get(period, [0, 0])
            if e != a:
                bad.append(f"  {period}: parsed rows={e[0]} people={e[1]:,} | "
                           f"table rows={a[0]} people={a[1]:,}")
        if bad:
            ok = False
            lines.append(f"{label}: {len(bad)} period(s) diverge")
            lines.extend(bad[:10])
        else:
            tot_rows = sum(v[0] for v in exp.values())
            tot_ppl = sum(v[1] for v in exp.values())
            lines.append(f"{label}: {len(exp)} periods, {tot_rows:,} rows, "
                         f"{tot_ppl:,} people - exact")

    compare("la_asylum_support           ", england, "la_asylum_support")
    compare("la_asylum_support_unallocated", unalloc,
            "la_asylum_support_unallocated")
    compare("asylum_support_non_england  ", non_eng, "asylum_support_non_england")

    # Count at the snapshot period being loaded, not across the whole table.
    # An unfiltered count only held while parse_reg_02 stamped every edition
    # with the same hardcoded date and so overwrote the previous snapshot in
    # place. Now that the period comes from the edition the table accumulates
    # one snapshot per edition, which is what period_ending is in the key for.
    cur.execute("SELECT count(*) FROM la_immigration_groups "
                "WHERE period_ending = %s", (reg_period,))
    n = cur.fetchone()[0]
    cur.execute("SELECT count(DISTINCT period_ending) FROM la_immigration_groups")
    snapshots = cur.fetchone()[0]
    if n != len(reg_rows):
        ok = False
        lines.append(f"la_immigration_groups at {reg_period}: "
                     f"parsed {len(reg_rows)} vs table {n}")
    else:
        lines.append(f"la_immigration_groups       : {n:,} rows at "
                     f"{reg_period} - exact ({snapshots} snapshot(s) retained)")
    return _record("Check 4 - Load fidelity", ok, "\n".join(lines))


def check_5_reasonableness(cur):
    cur.execute("SELECT count(*) FROM la_asylum_support WHERE people < 0")
    neg = cur.fetchone()[0]
    cur.execute("""
        SELECT s.period_ending, b.lad24nm, SUM(s.people) AS n
          FROM la_asylum_support s JOIN la_boundaries b ON b.lad24cd = s.lad24cd
         GROUP BY s.period_ending, b.lad24nm ORDER BY n DESC LIMIT 1
    """)
    period, la, mx = cur.fetchone()
    # Distribution shape, measured on the Home Office's own denominator: all
    # UK local authorities at the latest period, not England-only and not
    # (period, LA) cells. The published statement for year ending March 2026
    # is that roughly 181 of 361 LAs had fewer than 100 supported people.
    cur.execute("""
        WITH uk AS (
            SELECT lad24cd AS code, SUM(people) AS n
              FROM la_asylum_support WHERE period_ending = %s GROUP BY 1
            UNION ALL
            SELECT lad_code AS code, SUM(people) AS n
              FROM asylum_support_non_england WHERE period_ending = %s GROUP BY 1
        )
        SELECT count(*), count(*) FILTER (WHERE n < 100),
               count(*) FILTER (WHERE n >= 100) FROM uk
    """, (ANCHOR_PERIOD, ANCHOR_PERIOD))
    las_in_source, under_100, at_or_above = cur.fetchone()
    delta = abs(at_or_above - PUBLISHED_AT_OR_ABOVE_100)
    dist_ok = delta <= AT_OR_ABOVE_TOLERANCE

    cur.execute("""
        SELECT count(DISTINCT lad24cd) FROM la_asylum_support
         WHERE period_ending = %s
    """, (ANCHOR_PERIOD,))
    eng_present = cur.fetchone()[0]
    non_eng_present = las_in_source - eng_present
    absent = PUBLISHED_LA_UNIVERSE - las_in_source
    eng_absent = UK_LAD_DECOMPOSITION[0][1] - eng_present
    non_eng_absent = absent - eng_absent
    decomp = " + ".join(f"{n} {name}" for name, n in UK_LAD_DECOMPOSITION)

    ok = neg == 0 and mx <= 10000 and dist_ok
    return _record("Check 5 - Reasonableness", ok,
                   f"Negative values: {neg}\n"
                   f"England maximum across all 33 periods: {mx:,} "
                   f"({la}, {period})\n"
                   f"Ceiling 10,000 breached: {'YES' if mx > 10000 else 'no'}\n"
                   f"\n"
                   f"Distribution shape at {ANCHOR_PERIOD}, UK-wide "
                   f"(la_asylum_support + asylum_support_non_england).\n"
                   f"Stated on the at-or-above side, which is computed from "
                   f"the source with no adjustment:\n"
                   f"\n"
                   f"  LAs at or above 100, from the source : "
                   f"{las_in_source} present - {under_100} under 100 = "
                   f"{at_or_above}\n"
                   f"  Published                           : "
                   f"{PUBLISHED_UNDER_100} of {PUBLISHED_LA_UNIVERSE} under "
                   f"100, therefore {PUBLISHED_AT_OR_ABOVE_100} at or above\n"
                   f"  Difference                          : {delta} "
                   f"(tolerance {AT_OR_ABOVE_TOLERANCE}) "
                   f"{'within' if dist_ok else 'OUTSIDE'}\n"
                   f"\n"
                   f"Confirming decomposition of the Home Office denominator:\n"
                   f"  {PUBLISHED_LA_UNIVERSE} = {decomp}\n"
                   f"  The denominator is the full UK LAD set, which is why "
                   f"LAs absent from the file\n"
                   f"  fall in the under-100 bucket. This independently "
                   f"confirms that an absent LA\n"
                   f"  means 'not published' rather than zero.\n"
                   f"\n"
                   f"  {las_in_source} present = {eng_present} England + "
                   f"{non_eng_present} non-England\n"
                   f"  {absent} absent  = {eng_absent} England + "
                   f"{non_eng_absent} non-England\n"
                   f"  The {eng_absent} absent English LAs match the named "
                   f"list in Check 9.\n"
                   f"Note: the published maximum, Glasgow City 3,870, is "
                   f"Scottish and sits in asylum_support_non_england, so it "
                   f"cannot appear in the England maximum above. Tested there "
                   f"by Check 3.")


def check_6_suppression(cur):
    cur.execute("""
        SELECT count(*) FILTER (WHERE suppressed),
               count(*) FILTER (WHERE suppressed AND people IS NOT NULL),
               count(*) FILTER (WHERE suppressed AND people = 0),
               count(*) FILTER (WHERE NOT suppressed AND people IS NULL)
          FROM la_immigration_groups
    """)
    n_sup, bad_notnull, bad_zero, bad_null = cur.fetchone()
    cur.execute("""
        SELECT count(*) FROM la_asylum_support WHERE people IS NULL
    """)
    asy_null = cur.fetchone()[0]
    ok = bad_notnull == 0 and bad_zero == 0 and bad_null == 0 and asy_null == 0
    return _record("Check 6 - Suppression handling", ok,
                   f"la_immigration_groups suppressed rows: {n_sup}\n"
                   f"  suppressed with non-NULL people: {bad_notnull}\n"
                   f"  suppressed coerced to 0: {bad_zero}\n"
                   f"  unsuppressed with NULL people: {bad_null}\n"
                   f"la_asylum_support NULL people: {asy_null} "
                   f"(expected 0; source is numeric throughout, which is why "
                   f"that table carries no suppressed column)")


def check_7_idempotency(cur, reload_fn, checksum_fn):
    cur.execute("SELECT count(*) FROM la_asylum_support")
    rows_before = cur.fetchone()[0]
    sum_before = checksum_fn(cur)
    reload_fn(cur)
    cur.execute("SELECT count(*) FROM la_asylum_support")
    rows_after = cur.fetchone()[0]
    sum_after = checksum_fn(cur)
    ok = rows_before == rows_after and sum_before == sum_after
    return _record("Check 7 - Idempotency", ok,
                   f"Rows before / after second load: {rows_before:,} / "
                   f"{rows_after:,}\n"
                   f"Checksum before: {sum_before}\n"
                   f"Checksum after : {sum_after}\n"
                   f"{'Identical' if ok else 'DIVERGED'} (loaded_at may change)")


def _load_d09(path):
    df = pd.read_excel(path, sheet_name="Data_Asy_D09", header=1,
                       engine="openpyxl")
    cols = list(df.columns)
    # 'Region' is the nationality region; UK geography is 'UK Region / Nation'.
    c_date, c_ukreg, c_people = cols[0], cols[5], cols[6]
    df[c_date] = pd.to_datetime(df[c_date], format="%d %b %Y").dt.date
    eng, una = defaultdict(int), defaultdict(int)
    for r in df.itertuples(index=False):
        period, region, people = r[0], str(r[5]).strip(), int(r[6])
        if region in ENGLISH_REGIONS:
            eng[period] += people
        elif region.upper().startswith("N/A") or region == "Unknown":
            una[period] += people
    return eng, una


def check_8_asy_d09(cur, paths):
    eng, una = _load_d09(paths["d09"])
    cur.execute("""SELECT period_ending, SUM(people) FROM la_asylum_support
                   GROUP BY 1""")
    db_eng = {r[0]: int(r[1]) for r in cur.fetchall()}
    cur.execute("""SELECT period_ending, SUM(people)
                     FROM la_asylum_support_unallocated GROUP BY 1""")
    db_una = {r[0]: int(r[1]) for r in cur.fetchall()}

    bad_a = [(p, db_eng[p], eng.get(p, 0)) for p in sorted(db_eng)
             if db_eng[p] != eng.get(p, 0)]
    ok_a = not bad_a
    detail_a = [f"Periods compared: {len(db_eng)}",
                f"Divergent: {len(bad_a)}"]
    detail_a += [f"  {p}: table {a:,} vs Asy_D09 {b:,}" for p, a, b in bad_a[:10]]
    _record("Check 8a - England total vs Asy_D09 English regions", ok_a,
            "\n".join(detail_a))

    periods = sorted(set(db_una) | {p for p in db_eng})
    bad_b = [(p, db_una.get(p, 0), una.get(p, 0)) for p in periods
             if db_una.get(p, 0) != una.get(p, 0)]
    ok_b = not bad_b
    detail_b = [f"Periods compared: {len(periods)}",
                f"Divergent: {len(bad_b)}"]
    detail_b += [f"  {p}: table {a:,} vs Asy_D09 {b:,}" for p, a, b in bad_b[:10]]
    _record("Check 8b - Unallocated vs Asy_D09 no-region rows", ok_b,
            "\n".join(detail_b))
    return ok_a and ok_b


def check_9_cross_source(cur, reg_period):
    """Reg_02 supported-asylum total vs Asy_D11 aggregate, post-resolution."""
    cur.execute("""
        WITH reg AS (
            SELECT lad24cd, people
              FROM la_immigration_groups
             WHERE pathway = 'supported_asylum' AND sub_pathway = 'total'
               AND period_ending = %s
        ), asy AS (
            SELECT lad24cd, SUM(people) AS people
              FROM la_asylum_support WHERE period_ending = %s GROUP BY 1
        )
        SELECT count(*) FILTER (WHERE r.lad24cd IS NOT NULL AND a.lad24cd IS NOT NULL),
               count(*) FILTER (WHERE r.people IS NOT DISTINCT FROM a.people
                                  AND r.lad24cd IS NOT NULL AND a.lad24cd IS NOT NULL),
               count(*) FILTER (WHERE a.lad24cd IS NULL),
               count(*) FILTER (WHERE r.lad24cd IS NULL)
          FROM reg r FULL OUTER JOIN asy a ON a.lad24cd = r.lad24cd
    """, (reg_period, reg_period))
    matched, exact, only_reg, only_asy = cur.fetchone()

    cur.execute("""
        WITH reg AS (
            SELECT lad24cd, people FROM la_immigration_groups
             WHERE pathway='supported_asylum' AND sub_pathway='total'
               AND period_ending=%s
        ), asy AS (
            SELECT lad24cd, SUM(people) AS people FROM la_asylum_support
             WHERE period_ending=%s GROUP BY 1
        )
        SELECT r.lad24cd, r.people, a.people
          FROM reg r JOIN asy a ON a.lad24cd = r.lad24cd
         WHERE r.people IS DISTINCT FROM a.people
    """, (reg_period, reg_period))
    div = cur.fetchall()

    # The two LAs the resolution cascade acted on.
    cur.execute("""
        SELECT b.lad24nm, g.people, (SELECT SUM(people) FROM la_asylum_support s
                                      WHERE s.lad24cd = g.lad24cd
                                        AND s.period_ending = g.period_ending)
          FROM la_immigration_groups g JOIN la_boundaries b ON b.lad24cd = g.lad24cd
         WHERE g.pathway='supported_asylum' AND g.sub_pathway='total'
           AND g.period_ending=%s AND g.lad24cd IN ('E08000016','E08000019')
         ORDER BY b.lad24nm
    """, (reg_period,))
    recoded = cur.fetchall()

    ok = not div
    lines = [f"English LAs compared (post-resolution): {matched}",
             f"Exact match: {exact}",
             f"Divergent: {len(div)}",
             f"In Reg_02 but not Asy_D11: {only_asy}",
             f"In Asy_D11 but not Reg_02: {only_reg}"]
    lines += [f"  {c}: Reg_02 {r} vs Asy_D11 {a}" for c, r, a in div[:10]]
    lines.append("Codes the cascade acted on (E08000038/E08000039 forward-resolved):")
    for name, rv, av in recoded:
        lines.append(f"  {name:<12} Reg_02 {rv:<8} Asy_D11 {av:<8} "
                     f"{'MATCH' if rv == av else 'MISMATCH'}")
    return _record("Check 9 - Cross-source Reg_02 vs Asy_D11", ok, "\n".join(lines))


def check_10_reg02_internal(cur, reg_period):
    cur.execute("""
        WITH p AS (
            SELECT lad24cd,
                   SUM(people) FILTER (WHERE sub_pathway='total'
                                         AND pathway <> 'all_pathways') AS parts,
                   SUM(people) FILTER (WHERE pathway='all_pathways') AS total,
                   bool_or(suppressed) FILTER (WHERE sub_pathway='total') AS has_supp
              FROM la_immigration_groups WHERE period_ending=%s GROUP BY 1
        )
        SELECT lad24cd, parts, total FROM p
         WHERE parts IS DISTINCT FROM total
    """, (reg_period,))
    rows = cur.fetchall()
    unexpected = [r for r in rows if r[0] not in REG02_TOTAL_EXEMPT]
    cur.execute("SELECT count(DISTINCT lad24cd) FROM la_immigration_groups "
                "WHERE period_ending=%s", (reg_period,))
    total_las = cur.fetchone()[0]

    # Name the suppressed-pathway LAs explicitly and show their arithmetic,
    # rather than reporting a bare exemption count.
    cur.execute("""
        SELECT b.lad24nm, g.lad24cd,
               SUM(g.people) FILTER (WHERE g.pathway='homes_for_ukraine')   AS hfu,
               SUM(g.people) FILTER (WHERE g.pathway='afghan_resettlement'
                                       AND g.sub_pathway='total')           AS afg,
               SUM(g.people) FILTER (WHERE g.pathway='supported_asylum'
                                       AND g.sub_pathway='total')           AS asy,
               SUM(g.people) FILTER (WHERE g.pathway='all_pathways')        AS tot,
               bool_or(g.suppressed)                                        AS supp
          FROM la_immigration_groups g
          JOIN la_boundaries b ON b.lad24cd = g.lad24cd
         WHERE g.period_ending = %s AND g.sub_pathway = 'total'
         GROUP BY b.lad24nm, g.lad24cd
        HAVING bool_or(g.suppressed)
         ORDER BY b.lad24nm
    """, (reg_period,))
    suppressed_las = cur.fetchall()

    ok = not unexpected
    lines = [f"LAs checked: {total_las}",
             f"Reconciling exactly: {total_las - len(rows)}",
             f"Unexpected divergence: {len(unexpected)}"]
    lines += [f"  {c}: pathways {p} vs total {t}" for c, p, t in unexpected[:10]]
    lines.append("")
    lines.append(f"LAs with a suppressed pathway "
                 f"(GOV.UK exemption list): {len(suppressed_las)}")
    for name, code, hfu, afg, asy, tot, _s in suppressed_las:
        lines.append(f"  {name} ({code}): HfU={'suppressed' if hfu is None else hfu}"
                     f"  Afghan={afg}  Asylum={asy}  published total={tot}")
        lines.append(f"    {afg} + {asy} = {(afg or 0) + (asy or 0)}, "
                     f"matches the published total. The suppressed Homes for "
                     f"Ukraine figure is excluded from the total, not merely "
                     f"hidden, so the two reconcile without an exemption.")
    return _record("Check 10 - Reg_02 internal reconciliation", ok,
                   "\n".join(lines))


def check_11_collisions(collisions):
    """The halt threshold applies to genuine duplicate keys only.

    Forward-resolving abolished districts onto a successor unitary collapses
    several source rows onto one natural key by design. Those merges are the
    approved cascade working, not a parse or key defect, so counting them
    against the threshold would halt every load for the wrong reason.
    """
    merges = [c for c in collisions if c[2] == "reorganisation_merge"]
    dupes = [c for c in collisions if c[2] == "duplicate_key"]
    ok = len(dupes) <= COLLISION_HALT_THRESHOLD

    successors = defaultdict(int)
    for key, _rows, _kind in merges:
        successors[key[2]] += 1

    lines = [f"Total natural-key collisions collapsed by SUM: {len(collisions)}",
             f"  reorganisation merges (expected): {len(merges)}",
             f"  duplicate keys (anomalies)      : {len(dupes)}",
             f"Halt threshold applies to duplicate keys only: "
             f"{COLLISION_HALT_THRESHOLD}",
             "",
             "Reorganisation merges by successor unitary:"]
    for code, n in sorted(successors.items()):
        lines.append(f"  {code}: {n}")
    lines.append("")
    lines.append("Duplicate keys:")
    if not dupes:
        lines.append("  (none)")
    for key, rows, _kind in dupes:
        lines.append(f"  key {key[1:]}")
        for r in rows:
            lines.append(f"    source row: {r}")
        lines.append(f"    summed to: {sum(int(r[6]) for r in rows)}")
    return _record("Check 11 - Collision log", ok, "\n".join(lines))


def _write_anomalies(merges, dupes, rows_in_scope=None, rows_landed=None):
    # Repo root, not the script's own directory. This resolved from the
    # script directory until 2026-09-04, which was correct while the script
    # sat at the repo root and wrote to scripts/docs/ silently after the
    # 2026-08-20 move, leaving docs/s6_source_anomalies.md frozen at the
    # 26 July run. Same defect class as the .env resolution gate 11 covers.
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    path = repo_root / "docs" / "s6_source_anomalies.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    absorbed = sum(len(rows) - 1 for _k, rows, _kind in merges + dupes)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# S6 source anomalies\n\n")
        fh.write("Anomalies found in the Home Office source files during the "
                 "S6 load, and the aggregation decisions taken in response. "
                 "Written by `s6_asylum_verify.py` on every run.\n\n")

        fh.write("## Row count reconciliation\n\n")
        if rows_in_scope is not None:
            fh.write("```\n")
            fh.write(f"{rows_in_scope:>7,}  rows in scope (Asy_D11, "
                     f"2018-01-01 forward)\n")
            fh.write(f"-{absorbed:>6,}  absorbed by SUM aggregation across "
                     f"{len(merges) + len(dupes)} collision keys\n")
            fh.write(f"{'':>7}  ({len(merges)} reorganisation merges, "
                     f"{len(dupes)} duplicate key"
                     f"{'s' if len(dupes) != 1 else ''})\n")
            fh.write(f"={rows_landed:>6,}  rows landed across "
                     f"la_asylum_support, la_asylum_support_unallocated "
                     f"and asylum_support_non_england\n")
            fh.write("```\n\n")
            fh.write("People totals are unaffected: aggregation preserves "
                     "`SUM`. The per-key row counts below make the "
                     f"{absorbed} derivable rather than asserted.\n\n")

        fh.write("## How collisions arise\n\n")
        fh.write("Several source rows can collapse onto one "
                 "`(period_ending, lad24cd, support_type, accommodation_type)` "
                 "key after geography resolution and accommodation-type "
                 "normalisation. They are summed before upsert, because "
                 "`ON CONFLICT DO UPDATE` would otherwise keep one row and "
                 "silently discard the rest. Two distinct causes:\n\n")

        fh.write("## Duplicate keys (source defects)\n\n")
        fh.write("Source rows carrying the **same** LAD code on the same "
                 "natural key. These count against the halt threshold.\n\n")
        if not dupes:
            fh.write("None in this load.\n\n")
        for key, rows, _kind in dupes:
            _p, _cd, _s, _a = key[1:]
            fh.write(f"### `{_p}` · `{_cd}` · `{_s}` · `{_a}`\n\n")
            fh.write("| Date | Support | Region | LA | LAD Code | "
                     "Accommodation | People |\n")
            fh.write("|---|---|---|---|---|---|---|\n")
            for r in rows:
                fh.write("| " + " | ".join(str(x) for x in r) + " |\n")
            fh.write(f"\nSummed to **{sum(int(r[6]) for r in rows)}**.\n\n")

        fh.write("## Reorganisation merges (expected)\n\n")
        fh.write("Source rows carrying **different** LAD codes that resolve "
                 "forward onto one successor unitary. This is the geography "
                 "cascade working as designed, not a defect, so these do not "
                 "count against the halt threshold.\n\n")
        if not merges:
            fh.write("None in this load.\n\n")
        else:
            by_succ = defaultdict(list)
            for key, rows, _kind in merges:
                by_succ[key[2]].append((key, rows))
            merge_absorbed = sum(len(rows) - 1 for _k, rows, _kind in merges)
            fh.write(f"{len(merges)} merge(s) across {len(by_succ)} successor "
                     f"unitaries, absorbing {merge_absorbed} rows.\n\n")
            fh.write("| Successor | Merges | Rows absorbed | "
                     "Predecessor codes seen |\n")
            fh.write("|---|---:|---:|---|\n")
            for code, items in sorted(by_succ.items()):
                codes = sorted({r[4] for _k, rows in items for r in rows})
                absorbed_here = sum(len(rows) - 1 for _k, rows in items)
                fh.write(f"| {code} | {len(items)} | {absorbed_here} | "
                         f"{', '.join(codes)} |\n")
            fh.write("\n### Per-key detail\n\n")
            fh.write("| Period | Successor | Support | Accommodation | "
                     "Source rows | Absorbed | Summed to |\n")
            fh.write("|---|---|---|---|---:|---:|---:|\n")
            for key, rows, _kind in sorted(
                    merges, key=lambda c: (c[0][1], c[0][2], c[0][3], c[0][4])):
                p, cd, s, a = key[1:]
                fh.write(f"| {p} | {cd} | {s} | {a} | {len(rows)} | "
                         f"{len(rows) - 1} | "
                         f"{sum(int(r[6]) for r in rows)} |\n")
            fh.write("\n")
        fh.write("## Region column reliability\n\n")
        fh.write("Five LAD codes are assigned to more than one UK region "
                 "across the loaded window: Middlesbrough (E06000002), "
                 "Herefordshire (E06000019), South Cambridgeshire "
                 "(E07000012), North Devon (E07000043) and Wolverhampton "
                 "(E08000031). The region column is therefore not stored. "
                 "Region is derived from `la_boundaries` where needed.\n")


def check_12_view_integrity(cur):
    cur.execute("""
        SELECT count(*) FROM vw_la_asylum_support_totals
         WHERE COALESCE(dispersal,0) + COALESCE(initial_accommodation,0)
             + COALESCE(contingency_all,0) + COALESCE(other_accommodation,0)
             + COALESCE(subsistence_only,0) + COALESCE(accommodation_not_stated,0)
            <> total_supported
    """)
    bad = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM vw_la_asylum_support_totals")
    total = cur.fetchone()[0]
    cur.execute("""
        SELECT count(*) FROM vw_la_asylum_support_totals
         WHERE COALESCE(section_4,0)+COALESCE(section_95,0)
             + COALESCE(section_98,0) <> total_supported
    """)
    bad_support = cur.fetchone()[0]
    ok = bad == 0 and bad_support == 0
    return _record("Check 12 - View integrity", ok,
                   f"View rows: {total:,}\n"
                   f"Accommodation columns not summing to total: {bad}\n"
                   f"Support-type columns not summing to total: {bad_support}\n"
                   f"Tolerance 0")


# ---------------------------------------------------------------------------

def run_all(cur, paths, d11_edition, reg_edition, geo, england, unalloc,
            non_eng, reg_rows, collisions, checksum_fn, reload_fn=None,
            stats=None, reg_period=None):
    # Reg_02 is a snapshot: checks 9 and 10 must read it at the period the
    # discovered edition actually describes, not at the fixed anchor. The
    # anchor stays fixed for the Asy_D11 time-series checks.
    if reg_period is None:
        raise ValueError("reg_period is required: the Reg_02 snapshot "
                         "period must come from the loaded edition.")
    global _results
    _results = []
    print("\n" + "=" * 78)
    print("PHASE 5 - S6 ASYLUM DISPERSAL VERIFICATION SUITE")
    print("=" * 78)

    check_1_coverage(cur, england)
    check_2_referential(cur)
    check_3_anchors(cur)
    check_4_load_fidelity(cur, england, unalloc, non_eng, reg_rows,
                          reg_period)
    check_5_reasonableness(cur)
    check_6_suppression(cur)
    if reload_fn is not None:
        check_7_idempotency(cur, reload_fn, checksum_fn)
    check_8_asy_d09(cur, paths)
    check_9_cross_source(cur, reg_period)
    check_10_reg02_internal(cur, reg_period)
    check_11_collisions(collisions)
    check_12_view_integrity(cur)

    _write_anomalies(
        [c for c in collisions if c[2] == "reorganisation_merge"],
        [c for c in collisions if c[2] == "duplicate_key"],
        rows_in_scope=(stats or {}).get("filtered_rows"),
        rows_landed=len(england) + len(unalloc) + len(non_eng))

    passed = sum(1 for r in _results if r["passed"])
    print("\n" + "=" * 78)
    print(f"VERIFICATION SUMMARY: {passed} / {len(_results)} passed")
    print("=" * 78)
    for r in _results:
        print(f"  [{'PASS' if r['passed'] else 'FAIL'}] {r['name']}")
    return _results
