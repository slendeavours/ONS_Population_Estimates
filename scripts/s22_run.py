"""S22 phase runner — discovery, load, hard gates, idempotency.

Hard gates run inside the load transaction. Any failure rolls the whole
transaction back, so a failed build leaves the database in its pre-load
state and halts with the reason.
"""
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import s22_ctb_discover as disco          # noqa: E402
import s22_ctb_empties_build as b         # noqa: E402
from _db import get_conn                  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
REPORT_DIR = REPO / "build_reports"
STATE = REPORT_DIR / "s22_build_state.json"

# Reconciliation targets, printed on the MHCLG release page itself.
# Rounded as published; the tolerance is 0.5%.
RELEASE_PAGE_TARGETS = [
    dict(figure="total dwellings", column="total_dwellings", value=25_800_000,
         quote='"In England, there were a total of 25.8 million dwellings as '
               'of 10 September 2025"'),
    dict(figure="empty dwellings (all, excluding exempt)",
         column="empty_total", value=542_000,
         quote='"there were 542,000 dwellings recorded as empty for the '
               'purposes of council tax as of 10 September 2025"'),
    dict(figure="empty homes charged a premium",
         column="empty_homes_premium_count", value=153_000,
         quote='"153,000 dwellings being charged an Empty Homes Premium"'),
    dict(figure="second homes", column="second_homes", value=268_000,
         quote='"There were 268,000 dwellings recorded as second homes for '
               'the purposes of council tax"'),
    dict(figure="unoccupied exempt dwellings",
         column="unoccupied_exemptions_total", value=212_000,
         quote='"There were 212,000 dwellings that were receiving an '
               'exemption that were unoccupied"'),
]


def log(msg):
    print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


def run():
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    started = datetime.datetime.now(datetime.timezone.utc)
    warnings = []

    conn = get_conn()
    conn.autocommit = False

    # ── Phase 0 ────────────────────────────────────────────────────────────
    n, used, used_raw = b.phase0_source_number(conn)
    log(f"Phase 0: source numbers in use {used} -> assigning S{n}")

    # ── Phase 1 ────────────────────────────────────────────────────────────
    log("Phase 1: discovering Source A (Council Taxbase)")
    src_a = disco.discover_council_taxbase()
    log(f"  {src_a['release_title']} ({src_a['taxbase_year']}), revised "
        f"{(src_a['public_updated'] or '')[:10]}")
    log(f"  {src_a['url']}")
    log("Phase 1: discovering Source B (Table 615)")
    src_b = disco.discover_table_615()
    log(f"  {src_b['attachment_title']}")
    log(f"  {src_b['url']}")

    records, class_rows, england, table_meta, publication, recodes = \
        b.extract_council_taxbase(src_a, conn)
    log(f"  Source A: {len(records)} authority rows, "
        f"{len(class_rows)} exemption class rows")
    for r in recodes:
        msg = (f"{r['la_name']} published by MHCLG as {r['published']}, "
               f"resolved to {r['lad24cd']} via la_code_lookup "
               "(change_type 'recode', 1 April 2025)")
        warnings.append(msg)
        log(f"  geography: {msg}")

    rows615 = b.extract_table_615(src_b)
    rows615 = b.resolve_615_geography(conn, rows615)
    log(f"  Source B: {len(rows615)} district-year rows")

    sheets_a = disco._ctb_sheet_structure(src_a["path"])
    sheets_b = disco._615_sheet_structure(src_b["path"])
    report = disco.write_structure_report(
        src_a, src_b, table_meta, RELEASE_PAGE_TARGETS, sheets_a, sheets_b,
        b.UNOCCUPIED_CLASSES, n)
    log(f"Phase 1: wrote {report.relative_to(REPO)}")

    # ── Phase 2 / 3 + hard gates in one transaction ───────────────────────
    log("Phase 2/3: creating tables, loading, creating view")
    cur = b.load_all(conn, records, class_rows, rows615,
                     src_a["technical_notes_url"])

    gates = []

    def gate(num, name, ok, detail):
        gates.append(dict(num=num, name=name, ok=bool(ok), detail=detail))
        log(f"  gate {num} {'PASS' if ok else 'FAIL'}: {name} — {detail}")

    year = src_a["taxbase_year"]

    cur.execute("SELECT COUNT(*) FROM la_council_taxbase_empties "
                "WHERE taxbase_year = %s", (year,))
    cnt = cur.fetchone()[0]
    gate(1, "296 rows for the latest taxbase year", cnt == 296,
         f"{cnt} rows for taxbase_year {year}")

    cur.execute("""
        SELECT COUNT(*) FROM la_council_taxbase_empties e
         WHERE e.taxbase_year = %s
           AND NOT EXISTS (SELECT 1 FROM la_boundaries bd
                            WHERE bd.lad24cd = e.lad24cd)
    """, (year,))
    orphans = cur.fetchone()[0]
    gate(2, "every lad24cd exists in la_boundaries", orphans == 0,
         f"{orphans} orphan codes")

    recon = []
    for t in RELEASE_PAGE_TARGETS:
        cur.execute(
            f"SELECT SUM({t['column']}) FROM la_council_taxbase_empties "
            "WHERE taxbase_year = %s", (year,))
        loaded = cur.fetchone()[0] or 0
        diff_pct = abs(loaded - t["value"]) / t["value"] * 100
        recon.append(dict(t, loaded=int(loaded), diff_pct=round(diff_pct, 4),
                          ok=diff_pct <= 0.5, target_source="release page"))
    # Not printed on the release page: the publisher's own England row is the
    # target for these two, stated as such wherever they appear.
    for column, value in (("empty_6_months_plus",
                           england["empty_6_months_plus"]),
                          ("empty_under_6_months",
                           england["empty_under_6_months"])):
        cur.execute(
            f"SELECT SUM({column}) FROM la_council_taxbase_empties "
            "WHERE taxbase_year = %s", (year,))
        loaded = cur.fetchone()[0] or 0
        diff_pct = abs(loaded - value) / value * 100
        recon.append(dict(
            figure=column.replace("_", " "), column=column, value=int(value),
            quote="NOT FOUND on the release page — no national figure is "
                  "printed for this measure. Target is the publisher's own "
                  "England total row in the same workbook.",
            loaded=int(loaded), diff_pct=round(diff_pct, 4),
            ok=diff_pct <= 0.5, target_source="workbook England row"))

    bad = [r for r in recon if not r["ok"]]
    gate(3, "national reconciliation within 0.5%", not bad,
         "; ".join(f"{r['column']} loaded {r['loaded']:,} vs target "
                   f"{r['value']:,} ({r['diff_pct']}%)" for r in recon))

    cur.execute("""
        SELECT COUNT(*) FROM la_council_taxbase_empties
         WHERE taxbase_year = %s
           AND (total_dwellings < 0 OR empty_under_6_months < 0
                OR empty_6_months_plus < 0 OR empty_total < 0
                OR empty_homes_premium_count < 0 OR second_homes < 0
                OR unoccupied_exemptions_total < 0)
    """, (year,))
    neg = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM v_la_empty_homes_rates "
                "WHERE lte_rate_pct < 0 OR lte_rate_pct > 100")
    oob = cur.fetchone()[0]
    cur.execute("""
        SELECT COUNT(*) FROM la_vacant_dwellings_615
         WHERE vacant_dwellings < 0 OR long_term_vacant_dwellings < 0
    """)
    neg615 = cur.fetchone()[0]
    gate(5, "no negative counts, lte_rate_pct within 0-100",
         neg == 0 and oob == 0 and neg615 == 0,
         f"{neg} negative CTB counts, {neg615} negative 615 counts, "
         f"{oob} lte_rate_pct out of range")

    if any(not g["ok"] for g in gates):
        conn.rollback()
        conn.close()
        sys.exit("HALT: hard gate failed — transaction rolled back, database "
                 "left in its pre-load state. See the gate output above.")

    conn.commit()
    log("Phase 2/3: committed")

    # ── Gate 4: idempotency ────────────────────────────────────────────────
    log("Gate 4: re-running the full load for idempotency")
    cur = conn.cursor()

    def snapshot():
        out = {}
        cur.execute("""
            SELECT COUNT(*), SUM(total_dwellings), SUM(empty_6_months_plus),
                   SUM(empty_total), SUM(empty_homes_premium_count),
                   SUM(second_homes), SUM(unoccupied_exemptions_total),
                   SUM(empty_under_6_months)
              FROM la_council_taxbase_empties
        """)
        out["empties"] = cur.fetchone()
        cur.execute("SELECT COUNT(*), SUM(dwellings) "
                    "FROM la_ctb_exemption_classes")
        out["classes"] = cur.fetchone()
        cur.execute("SELECT COUNT(*), SUM(vacant_dwellings), "
                    "SUM(long_term_vacant_dwellings) "
                    "FROM la_vacant_dwellings_615")
        out["v615"] = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM ctb_series_breaks")
        out["breaks"] = cur.fetchone()
        return out

    before = snapshot()
    b.load_all(conn, records, class_rows, rows615,
               src_a["technical_notes_url"])
    conn.commit()
    after = snapshot()
    same = before == after
    gates.append(dict(num=4, name="idempotency: second full load changes "
                                  "nothing", ok=same,
                      detail=f"before {before} / after {after}"))
    log(f"  gate 4 {'PASS' if same else 'FAIL'}: "
        f"{'row counts and value sums identical' if same else 'DIVERGED'}")
    if not same:
        conn.close()
        sys.exit("HALT: idempotency gate failed — the second load changed the "
                 "data. Investigate before proceeding.")

    state = dict(
        source_number=n, taxbase_year=year,
        source_numbers_in_use=used_raw,
        src_a={k: (str(v) if isinstance(v, Path) else v)
               for k, v in src_a.items()},
        src_b={k: (str(v) if isinstance(v, Path) else v)
               for k, v in src_b.items()},
        publication=publication,
        england=england,
        reconciliation=recon,
        gates=gates,
        table_meta=table_meta,
        recodes_applied=recodes,
        exemption_classes_built=True,
        started_at=started.isoformat(),
        warnings=warnings,
    )
    STATE.write_text(json.dumps(state, indent=2, default=str),
                     encoding="utf-8")
    log(f"wrote {STATE.relative_to(REPO)}")
    conn.close()


if __name__ == "__main__":
    run()
