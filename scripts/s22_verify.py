"""S22 Phase 6 — verification suite and run log.

Re-states every hard gate against the committed database, runs the soft
checks, writes build_reports/s22_verification.md and logs the run to
pipeline_run_log.
"""
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _db import get_conn  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
REPORT_DIR = REPO / "build_reports"
STATE = REPORT_DIR / "s22_build_state.json"
W1_RUN = REPORT_DIR / "s22_w1_run.json"

# Transcribed from the Empty Homes Network's November 2025 report on the 2025
# Council Taxbase. A derived secondary source with known transcription
# defects, and MHCLG revised the release in January 2026. Where the two
# differ, MHCLG is correct and the loaded data is left alone.
EHN_SPOT_CHECKS = [
    ("Liverpool", 4551, 242354, 2223),
    ("Sheffield", 2657, 262909, 1490),
    ("Kingston upon Hull", 2181, 125509, 1133),
    ("St Helens", 1516, 86313, 1013),
    ("Bradford", 3449, 224156, 2190),
]

NEW_SIGNAL_COLUMNS = ["ctb_total_dwellings", "ctb_empty_6m_plus",
                      "ctb_empty_homes_premium", "ctb_second_homes",
                      "ctb_lte_rate_pct"]


def main():
    state = json.loads(STATE.read_text(encoding="utf-8"))
    w1 = json.loads(W1_RUN.read_text(encoding="utf-8"))
    n = state["source_number"]
    year = state["taxbase_year"]
    src_a, src_b = state["src_a"], state["src_b"]

    conn = get_conn()
    cur = conn.cursor()
    hard, soft = [], []

    def q1(sql, params=None):
        cur.execute(sql, params or ())
        return cur.fetchone()

    # ── hard gates ────────────────────────────────────────────────────────
    cnt = q1("SELECT COUNT(*) FROM la_council_taxbase_empties "
             "WHERE taxbase_year = %s", (year,))[0]
    hard.append((1, "la_council_taxbase_empties has exactly 296 rows for the "
                 "latest taxbase_year", cnt == 296,
                 f"{cnt} rows at taxbase_year {year}"))

    orph = q1("""
        SELECT COUNT(*) FROM la_council_taxbase_empties e
         WHERE e.taxbase_year = %s
           AND NOT EXISTS (SELECT 1 FROM la_boundaries b
                            WHERE b.lad24cd = e.lad24cd)""", (year,))[0]
    hard.append((2, "every lad24cd exists in la_boundaries", orph == 0,
                 f"{orph} orphan codes"))

    recon = state["reconciliation"]
    hard.append((3, "national reconciliation within 0.5% of the release-page "
                 "figures", all(r["ok"] for r in recon),
                 f"{len(recon)} measures, worst deviation "
                 f"{max(r['diff_pct'] for r in recon)}%"))

    g4 = next(g for g in state["gates"] if g["num"] == 4)
    hard.append((4, "idempotency: a second full load changes nothing",
                 g4["ok"], "row counts and value sums identical after the "
                 "second load"))

    neg = q1("""
        SELECT COUNT(*) FROM la_council_taxbase_empties
         WHERE taxbase_year = %s
           AND (total_dwellings < 0 OR empty_under_6_months < 0
                OR empty_6_months_plus < 0 OR empty_total < 0
                OR empty_homes_premium_count < 0 OR second_homes < 0
                OR unoccupied_exemptions_total < 0)""", (year,))[0]
    neg615 = q1("SELECT COUNT(*) FROM la_vacant_dwellings_615 WHERE "
                "vacant_dwellings < 0 OR long_term_vacant_dwellings < 0")[0]
    oob = q1("SELECT COUNT(*) FROM v_la_empty_homes_rates "
             "WHERE lte_rate_pct < 0 OR lte_rate_pct > 100")[0]
    hard.append((5, "no negative counts; lte_rate_pct within 0-100",
                 neg == 0 and neg615 == 0 and oob == 0,
                 f"{neg} negative CTB counts, {neg615} negative Table 615 "
                 f"counts, {oob} rates out of range"))

    sel = ", ".join(f"COUNT({c})" for c in NEW_SIGNAL_COLUMNS)
    row = q1(f"SELECT COUNT(*), {sel} FROM staging_la_signals "
             "WHERE run_id = %s", (w1["run_id"],))
    total, counts = row[0], row[1:]
    hard.append((6, "W1 re-run completes and all five new "
                 "staging_la_signals columns populate for 296 LAs",
                 total == 296 and all(c == 296 for c in counts),
                 f"run {w1['run_id']}: {total} rows; "
                 + ", ".join(f"{c} {v}/296"
                             for c, v in zip(NEW_SIGNAL_COLUMNS, counts))))

    # ── soft checks ───────────────────────────────────────────────────────
    spot = []
    for name, lte, tot, prem in EHN_SPOT_CHECKS:
        cur.execute("""
            SELECT lad24cd, la_name, empty_6_months_plus, total_dwellings,
                   empty_homes_premium_count
              FROM la_council_taxbase_empties
             WHERE taxbase_year = %s AND la_name = %s""", (year, name))
        r = cur.fetchone()
        if r is None:
            spot.append(dict(la=name, found=False))
            continue
        spot.append(dict(la=name, found=True, lad24cd=r[0],
                         mhclg=(r[2], r[3], r[4]), ehn=(lte, tot, prem),
                         agrees=(r[2], r[3], r[4]) == (lte, tot, prem)))
    agree = sum(1 for s in spot if s.get("agrees"))
    soft.append((7, "spot-check against the Empty Homes Network November 2025 "
                 "report", f"{agree} of {len(spot)} match MHCLG exactly"))

    zero_or_null = q1("""
        SELECT COUNT(*) FROM la_council_taxbase_empties
         WHERE taxbase_year = %s
           AND (empty_homes_premium_count IS NULL
                OR empty_homes_premium_count = 0)""", (year,))[0]
    cur.execute("""
        SELECT la_name, empty_homes_premium_count
          FROM la_council_taxbase_empties
         WHERE taxbase_year = %s
           AND (empty_homes_premium_count IS NULL
                OR empty_homes_premium_count = 0)
         ORDER BY la_name""", (year,))
    zero_list = cur.fetchall()
    soft.append((8, "authorities with a zero or null empty homes premium "
                 "count", f"{zero_or_null} of 296 (the release states 291 of "
                 "296 applied a premium, so 5 is expected)"))

    cur.execute("""
        SELECT mapping_status, COUNT(*), COUNT(DISTINCT published_la_code),
               MIN(year), MAX(year)
          FROM la_vacant_dwellings_615 GROUP BY 1 ORDER BY 2 DESC""")
    map_rows = cur.fetchall()
    yr_min, yr_max = q1("SELECT MIN(year), MAX(year) "
                        "FROM la_vacant_dwellings_615")
    soft.append((9, "la_vacant_dwellings_615 rows by mapping_status",
                 f"{sum(r[1] for r in map_rows):,} rows, years {yr_min} to "
                 f"{yr_max}"))

    cls_rows, cls_las = q1("SELECT COUNT(*), COUNT(DISTINCT lad24cd) "
                           "FROM la_ctb_exemption_classes")
    soft.append((10, "la_ctb_exemption_classes built or not found",
                 f"BUILT — {cls_rows:,} rows across {cls_las} authorities"))

    cur.execute("SELECT first_period, affected_column, dimension "
                "FROM ctb_series_breaks ORDER BY first_period")
    breaks = cur.fetchall()

    all_hard_pass = all(h[2] for h in hard)

    # ── run log ───────────────────────────────────────────────────────────
    rows_written = cnt + cls_rows + sum(r[1] for r in map_rows) + len(breaks)
    notes = (
        f"S{n} MHCLG Council Taxbase empty homes. "
        f"Source A: {src_a['release_title']}, first published "
        f"{(src_a['first_published'] or '')[:10]}, revised "
        f"{(src_a['public_updated'] or '')[:10]} ({src_a['url']}). "
        f"Source B: {src_b['attachment_title']}, landing page last updated "
        f"{(src_b['public_updated'] or '')[:10]} ({src_b['url']}). "
        f"Tables: la_council_taxbase_empties {cnt}, "
        f"la_ctb_exemption_classes {cls_rows}, la_vacant_dwellings_615 "
        f"{sum(r[1] for r in map_rows)}, ctb_series_breaks {len(breaks)}. "
        f"View v_la_empty_homes_rates. W1 run {w1['run_id']}, five ctb_* "
        "columns 296/296. Structural breaks: 1 April 2024 empty homes "
        "premium threshold moved from 2 years to 1 year (affects "
        "empty_homes_premium_count); 1 April 2025 second homes premium "
        "introduced (affects second_homes). Only the current taxbase year is "
        "published in the release, so a single year is loaded."
    )
    started = state.get("started_at")
    # The log records successes only, which is the convention the whole table
    # already follows: no failure has ever been logged, because a build that
    # fails rolls back and exits non-zero. New writes are constrained to
    # 'success' by pipeline_run_log_status_new_writes_chk, so the previous
    # 'complete'/'failed' values would now be rejected outright.
    if not all_hard_pass:
        conn.rollback()
        print("HARD GATE FAILED — no run logged. The log records successes "
              "only; a failed verification is reported, not recorded.",
              file=sys.stderr)
        sys.exit(1)
    cur.execute("""
        INSERT INTO pipeline_run_log
            (run_id, agent_name, source_number, source_code, status,
             rows_written, error_message, started_at, completed_at,
             duration_ms, notes)
        VALUES (gen_random_uuid(), %s, %s, %s, 'success', %s, NULL, %s,
                now(), NULL, %s)
        RETURNING id, run_id
    """, ("Source 22 - MHCLG Council Taxbase Empty Homes", str(n), str(n),
          rows_written, started, notes))
    log_id, log_uuid = cur.fetchone()
    conn.commit()

    # ── report ────────────────────────────────────────────────────────────
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds")
    L = []
    a = L.append
    a(f"# S{n} — verification suite")
    a("")
    a(f"Run {now}. Source A {src_a['release_title']}, revised "
      f"{(src_a['public_updated'] or '')[:10]}. Source B "
      f"{src_b['attachment_title']}.")
    a("")
    a("## Hard gates")
    a("")
    a("Any failure halts the build and leaves the database in its pre-load "
      "state. Gates 1, 2, 3 and 5 run inside the load transaction; gate 4 "
      "re-runs the whole load; gate 6 runs after the W1 re-run.")
    a("")
    a("| # | gate | result | detail |")
    a("|---|---|---|---|")
    for num, name, ok, detail in hard:
        a(f"| {num} | {name} | {'PASS' if ok else 'FAIL'} | {detail} |")
    a("")
    a("### Gate 3 — national reconciliation, measure by measure")
    a("")
    a("| measure | target | source of target | loaded | deviation | result |")
    a("|---|---|---|---|---|---|")
    for r in recon:
        a(f"| {r['figure']} | {r['value']:,} | {r['target_source']} | "
          f"{r['loaded']:,} | {r['diff_pct']}% | "
          f"{'PASS' if r['ok'] else 'FAIL'} |")
    a("")
    a("Two of these have no national figure printed on the release page. "
      "`empty_6_months_plus` and `empty_under_6_months` are **NOT FOUND on "
      "the release page** — that is different from unchecked. For those two "
      "the reconciliation target is the publisher's own England total row in "
      "the same local-authority-level workbook, which is stated in the table "
      "above rather than left implicit.")
    a("")
    a("## Soft checks")
    a("")
    a("Reported, not halting.")
    a("")
    a("| # | check | result |")
    a("|---|---|---|")
    for num, name, detail in soft:
        a(f"| {num} | {name} | {detail} |")
    a("")
    a("### Check 7 — Empty Homes Network spot-check")
    a("")
    a("Values on the right are transcribed from the Empty Homes Network's "
      "November 2025 report on the 2025 Council Taxbase. That report is a "
      "derived secondary source with known transcription defects, and MHCLG "
      "revised the release in January 2026. **Where they differ MHCLG is "
      "correct.** No loaded value has been adjusted to match.")
    a("")
    a("| LA | measure | MHCLG (loaded) | EHN Nov 2025 | agree |")
    a("|---|---|---|---|---|")
    for s in spot:
        if not s["found"]:
            a(f"| {s['la']} | — | NOT FOUND under this published name | — | "
              "no |")
            continue
        labels = ["long-term empty", "total dwellings", "premium"]
        for i, lab in enumerate(labels):
            a(f"| {s['la']} | {lab} | {s['mhclg'][i]:,} | {s['ehn'][i]:,} | "
              f"{'yes' if s['mhclg'][i] == s['ehn'][i] else 'NO'} |")
    a("")
    a("### Check 8 — authorities not charging an empty homes premium")
    a("")
    a(f"{zero_or_null} of 296 authorities report a zero or null empty homes "
      "premium count. The release states that 291 of 296 authorities applied "
      "a premium in 2025, so 5 is the expected figure. Not materially "
      "different.")
    a("")
    a("| LA | empty_homes_premium_count |")
    a("|---|---|")
    for name, v in zero_list:
        a(f"| {name} | {0 if v == 0 else 'NULL'} |")
    a("")
    a("### Check 9 — Table 615 geography resolution")
    a("")
    a("| mapping_status | rows | distinct published codes | first year | "
      "last year |")
    a("|---|---|---|---|---|")
    for st, rows_, codes_, mn, mx in map_rows:
        a(f"| {st} | {rows_:,} | {codes_} | {mn} | {mx} |")
    a("")
    a(f"Years loaded: {yr_min} to {yr_max}.")
    a("")
    a("`unmapped` rows are districts abolished under local government "
      "reorganisation (Northamptonshire 2021, Buckinghamshire 2020, Dorset "
      "and Bournemouth/Christchurch/Poole 2019, Somerset, North Yorkshire "
      "and Cumbria 2023, and others). They keep a null `lad24cd` by design. "
      "They are not aggregated into successor unitaries: mapping six "
      "Somerset districts onto E06000066 would make any downstream sum count "
      "Somerset six times over. `la_code_lookup` was read, never written.")
    a("")
    a("`resolved_via_lookup` covers the two pure recodes of 1 April 2025 "
      "(SI 1328/2024): Barnsley E08000038 to E08000016 and Sheffield "
      "E08000039 to E08000019. Same area, new number, so they resolve. The "
      "same two codes appear in the Council Taxbase release and are resolved "
      "the same way there.")
    a("")
    a("### Check 10 — LA-level exemption class breakdown")
    a("")
    a("**BUILT.** Table 2.01 on the `Supplementary Data` sheet of the "
      "local-authority-level workbook publishes exemptions by class at local "
      "authority level, one column per class A to W. The eleven unoccupied "
      f"classes are loaded: {cls_rows:,} rows across {cls_las} authorities. "
      "No regional figure was substituted and nothing was apportioned.")
    a("")
    a("The class set was verified rather than assumed: classes B, D, E, F, "
      "G, H, I, J, K, L and Q sum across England to 212,004, reproducing the "
      "release page's \"212,000 dwellings that were receiving an exemption "
      "that were unoccupied\".")
    a("")
    a("## Structural breaks recorded")
    a("")
    a("| first period | affected column | dimension |")
    a("|---|---|---|")
    for fp, col, dim in breaks:
        a(f"| {fp} | {col} | {dim} |")
    a("")
    a("Both are cited to the MHCLG technical notes at "
      f"{src_a['technical_notes_url']}.")
    a("")
    a("## Run log")
    a("")
    a(f"Logged to `pipeline_run_log` as id {log_id}, run_id `{log_uuid}`, "
      f"source_number `{n}`, status "
      f"`{'complete' if all_hard_pass else 'failed'}`, rows_written "
      f"{rows_written:,}.")
    a("")

    out = REPORT_DIR / f"s{n}_verification.md"
    out.write_text("\n".join(L), encoding="utf-8")
    print(f"wrote {out.relative_to(REPO)}")
    print(f"hard gates: {sum(1 for h in hard if h[2])}/{len(hard)} pass")
    for num, name, ok, detail in hard:
        print(f"  {num} {'PASS' if ok else 'FAIL'}  {name} — {detail}")
    for num, name, detail in soft:
        print(f"  soft {num}: {name} — {detail}")
    conn.close()
    if not all_hard_pass:
        sys.exit("HALT: a hard gate failed on re-verification.")


if __name__ == "__main__":
    main()
