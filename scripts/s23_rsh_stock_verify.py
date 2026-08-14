"""S23 verification suite - seven hard gates. Any failure aborts.

Never commits. Reads through get_readonly_conn(); the idempotency check
re-runs the real upsert inside a transaction that is rolled back in a finally
block. The upsert SQL is imported from the build module rather than copied.

Usage:
    python scripts/s23_rsh_stock_verify.py
"""
import sys
from pathlib import Path

import openpyxl
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _db import get_conn, get_readonly_conn, readonly_identity  # noqa: E402
import s23_rsh_stock_build as build  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TABLE = build.TABLE
UNIVERSE = 296

RESULTS = []


def gate(n, name, passed, detail):
    RESULTS.append(passed)
    print(f"[{'PASS' if passed else 'FAIL'}] Gate {n}: {name}")
    for line in str(detail).splitlines():
        print(f"        {line}")


def checksum(cur):
    cur.execute(f"""
        SELECT md5(string_agg(line, '' ORDER BY line))
        FROM (
            SELECT stock_date::text || '|' || rp_code || '|' || lad24cd || '|' ||
                   total_social_stock::text || '|' ||
                   supported_housing_and_older_people::text || '|' ||
                   source_url AS line
            FROM {TABLE}
        ) s
    """)
    return cur.fetchone()[0]


def main():
    ro_user, dedicated = readonly_identity()
    print(f"S23 verification suite - {TABLE}")
    print(f"connection: {ro_user}"
          f"{' (dedicated read-only role)' if dedicated else ' (session read-only)'}")
    print()

    edition = build.resolve_edition()
    path = build.fetch(edition)
    rows, idx = build.read_sheet(path)
    provider, la_totals, other = build.split_rows(rows, idx)

    conn = get_readonly_conn()
    cur = conn.cursor()

    # ---- Gate 1: row count against the source file -----------------------
    cur.execute(f"SELECT COUNT(*) FROM {TABLE} WHERE stock_date = %s",
                (edition["stock_date"],))
    loaded = cur.fetchone()[0]
    gate(1, "row count matches the provider rows in the source sheet",
         loaded == len(provider),
         f"source sheet {build.SHEET} in {edition['filename']}\n"
         f"  provider rows (RP_Type in Large/Small/LARP): {len(provider)}\n"
         f"  LA subtotal rows (RP_Type = 'LA'), not loaded by design: "
         f"{len(la_totals)}\n"
         f"  regional aggregate rows, not loaded by design: {len(other)}\n"
         f"  loaded for stock_date {edition['stock_date']}: {loaded}")

    # ---- Gate 2: geographic coverage -------------------------------------
    cur.execute(f"SELECT COUNT(DISTINCT lad24cd) FROM {TABLE} WHERE stock_date = %s",
                (edition["stock_date"],))
    n_la = cur.fetchone()[0]
    detail = [f"{n_la}/{UNIVERSE} English local authorities carry at least one "
              f"registered provider"]
    if n_la != UNIVERSE:
        cur.execute(f"""
            SELECT l.old_code, l.la_name FROM la_code_lookup l
            WHERE l.change_type = 'current'
              AND l.old_code NOT IN (SELECT DISTINCT lad24cd FROM {TABLE})
            ORDER BY l.la_name
        """)
        detail += [f"absent: {name} ({code})" for code, name in cur.fetchall()]
    cur.execute(f"""
        SELECT COUNT(DISTINCT lad24cd) FROM {TABLE}
        WHERE stock_date = %s AND supported_housing_and_older_people > 0
    """, (edition["stock_date"],))
    with_supported = cur.fetchone()[0]
    detail.append(f"{with_supported}/{UNIVERSE} carry supported housing stock "
                  f"above zero")
    gate(2, "geographic coverage is the full 296-authority universe",
         n_la == UNIVERSE, "\n".join(detail))

    # ---- Gate 3: codes resolved through la_code_lookup -------------------
    cur.execute(f"""
        SELECT DISTINCT t.publisher_la_code FROM {TABLE} t
        WHERE NOT EXISTS (SELECT 1 FROM la_code_lookup l
                          WHERE l.old_code = t.publisher_la_code)
        ORDER BY 1
    """)
    unresolved = [r[0] for r in cur.fetchall()]
    cur.execute(f"""
        SELECT DISTINCT t.publisher_la_code, t.lad24cd FROM {TABLE} t
        JOIN la_code_lookup l ON l.old_code = t.publisher_la_code
        WHERE l.new_code IS DISTINCT FROM t.lad24cd
    """)
    misresolved = cur.fetchall()
    gate(3, "every publisher LA code resolves through la_code_lookup",
         not unresolved and not misresolved,
         f"unresolved codes: {unresolved or 'none'}\n"
         f"codes stored against a lad24cd the lookup does not give: "
         f"{misresolved or 'none'}")

    # ---- Gate 4: per-row provenance --------------------------------------
    cur.execute(f"""
        SELECT COUNT(*) FROM {TABLE}
        WHERE source_url IS NULL OR source_url = ''
           OR source_file IS NULL OR source_file = ''
           OR release_page_url IS NULL OR release_page_url = ''
           OR edition IS NULL OR publication_date IS NULL OR stock_date IS NULL
    """)
    blank = cur.fetchone()[0]
    cur.execute(f"""
        SELECT edition, stock_date, publication_date, source_file, COUNT(*)
        FROM {TABLE} GROUP BY 1,2,3,4 ORDER BY 2
    """)
    eds = cur.fetchall()
    gate(4, "per-row source provenance is populated on every row",
         blank == 0,
         f"rows missing a provenance field: {blank}\n" +
         "\n".join(f"{e} | stock {s} | published {p} | {f} ({n} rows)"
                   for e, s, p, f, n in eds))

    # ---- Gate 5: suppression -------------------------------------------
    # This source publishes no suppression or missing-data markers at all, and
    # that is asserted rather than assumed: every stock cell in the sheet is
    # read and must be numeric or empty. Empty is then proved to mean zero
    # rather than unknown, because the four component columns sum exactly to
    # the publisher's own total on every row - which they could not do if a
    # blank stood for a withheld figure.
    markers, blanks, checked = {}, 0, 0
    for r in provider:
        for col in ["total_social_stock"] + build.STOCK_COLS:
            v = r[idx[col]]
            checked += 1
            if v is None:
                blanks += 1
            elif not isinstance(v, (int, float)):
                markers[str(v).strip()] = markers.get(str(v).strip(), 0) + 1
    cur.execute(f"""
        SELECT COUNT(*) FROM {TABLE} WHERE stock_date = %s
          AND total_social_stock <> general_needs_self_contained
                                  + general_needs_bedspaces
                                  + supported_housing_and_older_people
                                  + low_cost_home_ownership
    """, (edition["stock_date"],))
    broken = cur.fetchone()[0]
    cur.execute(f"""
        SELECT COUNT(*) FROM {TABLE} WHERE stock_date = %s
          AND (total_social_stock IS NULL
               OR supported_housing_and_older_people IS NULL)
    """, (edition["stock_date"],))
    nulls = cur.fetchone()[0]
    gate(5, "no value is silently coerced; blank is proved to mean zero",
         not markers and broken == 0 and nulls == 0,
         f"stock cells inspected in the source: {checked}\n"
         f"non-numeric markers found: "
         f"{markers or 'none - this sheet uses no suppression or missing-data notation'}\n"
         f"blank cells (read as zero): {blanks}\n"
         f"rows where the four components do not sum to the published total: "
         f"{broken} (must be 0 - this is what proves blank means zero)\n"
         f"NULL stock values stored: {nulls}")

    cur.close()
    conn.close()

    # ---- Gate 6: idempotency, always rolled back -------------------------
    probe = get_conn()
    pcur = probe.cursor()
    try:
        before_sum = checksum(pcur)
        pcur.execute(f"SELECT COUNT(*) FROM {TABLE}")
        before_n = pcur.fetchone()[0]
        out, _, _ = build.build_rows(pcur, edition, rows, idx)
        psycopg2.extras.execute_values(pcur, build.UPSERT, out, page_size=1000)
        after_sum = checksum(pcur)
        pcur.execute(f"SELECT COUNT(*) FROM {TABLE}")
        after_n = pcur.fetchone()[0]
        pcur.execute(f"""
            SELECT COUNT(*) FROM {TABLE} a
            JOIN {TABLE} b USING (stock_date, rp_code, lad24cd)
            WHERE a.total_social_stock IS DISTINCT FROM b.total_social_stock
               OR a.supported_housing_and_older_people
                   IS DISTINCT FROM b.supported_housing_and_older_people
        """)
        selfdiff = pcur.fetchone()[0]
        gate(6, "reloading changes no row and no cell",
             before_n == after_n and before_sum == after_sum and selfdiff == 0,
             f"rows before: {before_n}\n"
             f"rows after re-upserting {len(out)} rows: {after_n}\n"
             f"content checksum before: {before_sum}\n"
             f"content checksum after:  {after_sum}\n"
             f"cells differing (IS DISTINCT FROM): {selfdiff}")
    finally:
        probe.rollback()
        pcur.close()
        probe.close()

    # ---- Gate 7: reconciliation to the publisher's own subtotals ---------
    # The sheet carries 296 LA subtotal rows the publisher computed itself.
    # Every loaded authority must sum to its subtotal exactly, on all five
    # measures. This is a real reconciliation rather than an invented one:
    # the comparison is against arithmetic the publisher published.
    conn = get_readonly_conn()
    cur = conn.cursor()
    cur.execute(f"""
        SELECT publisher_la_code,
               SUM(total_social_stock), SUM(general_needs_self_contained),
               SUM(general_needs_bedspaces),
               SUM(supported_housing_and_older_people),
               SUM(low_cost_home_ownership)
        FROM {TABLE} WHERE stock_date = %s
        GROUP BY publisher_la_code
    """, (edition["stock_date"],))
    loaded_sums = {r[0]: list(r[1:]) for r in cur.fetchall()}
    cols = ["total_social_stock"] + build.STOCK_COLS
    breaches, compared = [], 0
    for code, row in la_totals.items():
        published = [build.num(row[idx[c]], c) for c in cols]
        got = loaded_sums.get(code)
        if got is None:
            breaches.append(f"{code}: publisher has a subtotal, nothing loaded")
            continue
        compared += 1
        if published != got:
            breaches.append(f"{code}: published {published} vs loaded {got}")
    nat_total = sum(v[0] for v in loaded_sums.values())
    nat_supported = sum(v[3] for v in loaded_sums.values())
    gate(7, "loaded rows reconcile to the publisher's own LA subtotals",
         not breaches and compared == UNIVERSE,
         f"authorities compared against the publisher's subtotal rows: "
         f"{compared}/{UNIVERSE}\n"
         f"disagreements: {breaches or 'none'}\n"
         f"national total social stock from loaded rows: {nat_total:,}\n"
         f"national supported housing and older people:  {nat_supported:,}\n"
         f"note: the publisher's headline national figures in the additional "
         f"tables are weighted to impute for small providers that submit the "
         f"short SDR form, so they are slightly higher than these unweighted "
         f"sums and are not asserted equal here.")

    cur.close()
    conn.close()

    print()
    passed = sum(1 for r in RESULTS if r)
    print(f"{passed}/{len(RESULTS)} gates passed")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
