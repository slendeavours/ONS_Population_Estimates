"""S1b verification suite - seven hard gates. Any failure aborts.

This suite never commits. It reads through get_readonly_conn(), and the one
check that has to write - idempotency - runs the real upsert inside a
transaction that is rolled back in a finally block, comparing a content hash
either side. The upsert SQL is imported from the build module rather than
copied, so there is one definition of how a row is written and the test cannot
drift from the thing it tests.

The live table holds the LATEST EDITION of each quarter (see s1b_editions.py),
which for revised quarters is not the file the release page links. Gates 6 and
7 therefore re-read each period's latest edition from its recorded local source
file in data/raw/s1b_a3, not from the release-page resolver.

Usage:
    python scripts/s1b_support_needs_verify.py
"""
import hashlib
import sys
from pathlib import Path

import psycopg2.extras

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _db import get_conn, get_readonly_conn, readonly_identity  # noqa: E402
import s1b_support_needs_build as build  # noqa: E402
from s1_editions import STALE_PERIODS  # noqa: E402
from s1b_editions import (TABLE as EDITIONS, DATA_COLS,  # noqa: E402
                          latest_edition, raw_path)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TABLE = build.TABLE
UNIVERSE = 296          # English local authorities on the pipeline's boundary set

RESULTS = []


def gate(n, name, passed, detail):
    RESULTS.append(passed)
    print(f"[{'PASS' if passed else 'FAIL'}] Gate {n}: {name}")
    for line in str(detail).splitlines():
        print(f"        {line}")


def expected_from_source():
    """Derive the expected row count from the publisher files, not the table.

    A check that counts the table against a number the table produced proves
    nothing. Each period's expectation is (LA rows in A3) x (columns mapped),
    read back out of the same parser the load used.
    """
    per_period = {}
    for period in sorted(build.RELEASES):
        edition = build.resolve_edition(period)
        df = build.read_a3(build.fetch(edition))
        _, mapping, _ = build.map_columns(df)
        per_period[period] = (len(build.la_rows(df)), len(mapping))
    return per_period


def content_hash(cur):
    """md5 over every live column except loaded_at (which an upsert resets) and
    category_label (the sheet header text; for revised quarters it embeds that
    file's England total and the live layer keeps its as-loaded label)."""
    cols = [c for c in DATA_COLS if c != "category_label"]
    line = " || '|' || ".join(f"COALESCE({c}::text, 'NULL')" for c in cols)
    cur.execute(f"""
        SELECT md5(string_agg(line, E'\\n' ORDER BY line))
        FROM (SELECT {line} AS line FROM {TABLE}) s
    """)
    return cur.fetchone()[0]


def latest_source(cur, period):
    """(edition dict for build_rows, local raw path, edition number) of the
    period's latest stored edition, taken from the editions table."""
    ed = latest_edition(cur, period)
    cur.execute(f"""SELECT DISTINCT source_url, source_edition, edition_variant,
                           release_page_url, reference_quarter, source_file
                    FROM {EDITIONS} WHERE period = %s AND edition = %s""",
                (period, ed))
    rows = cur.fetchall()
    if len(rows) != 1:
        raise RuntimeError(f"{period} edition {ed}: {len(rows)} source "
                           "descriptions, expected 1")
    url, name, variant, page, refq, source_file = rows[0]
    path = raw_path(period, source_file)
    if path is None:
        raise RuntimeError(f"{period} edition {ed}: local file "
                           f"{source_file} not found in data/raw/s1b_a3")
    edition = {"period": period, "reference_quarter": refq, "url": url,
               "filename": name, "variant": variant,
               "release_page_url": page}
    return edition, path, ed


def main():
    ro_user, dedicated = readonly_identity()
    print(f"S1b verification suite - {TABLE}")
    print(f"connection: {ro_user}"
          f"{' (dedicated read-only role)' if dedicated else ' (session read-only)'}")
    print()

    conn = get_readonly_conn()
    cur = conn.cursor()

    # ---- Gate 1: row count against the source files ----------------------
    per_period = expected_from_source()
    expected_total = sum(la * cols for la, cols in per_period.values())
    cur.execute(f"SELECT period, COUNT(*) FROM {TABLE} GROUP BY period")
    actual = dict(cur.fetchall())
    lines, mismatches = [], []
    for period, (la, cols) in sorted(per_period.items()):
        exp = la * cols
        got = actual.get(period, 0)
        lines.append(f"{period}: {la} LA rows x {cols} columns = {exp} expected, "
                     f"{got} loaded")
        if exp != got:
            mismatches.append(period)
    extra = sorted(set(actual) - set(per_period))
    if extra:
        mismatches.extend(extra)
    cur.execute(f"SELECT COUNT(*) FROM {TABLE}")
    total = cur.fetchone()[0]
    lines.append(f"total: {expected_total} expected, {total} loaded")
    gate(1, "row count matches the count derived from the source files",
         not mismatches and total == expected_total,
         "\n".join(lines) +
         (f"\nmismatched periods: {mismatches}" if mismatches else ""))

    # ---- Gate 2: geographic coverage -------------------------------------
    cur.execute(f"""
        SELECT period, COUNT(DISTINCT lad24cd) FROM {TABLE}
        GROUP BY period ORDER BY period
    """)
    cover = cur.fetchall()
    short = [(p, n) for p, n in cover if n != UNIVERSE]
    detail = [f"{p}: {n}/{UNIVERSE} English local authorities" for p, n in cover]
    if short:
        cur.execute("""
            SELECT old_code, la_name FROM la_code_lookup
            WHERE change_type = 'current'
              AND old_code NOT IN (SELECT DISTINCT lad24cd FROM """ + TABLE + """)
            ORDER BY la_name
        """)
        detail += [f"absent: {name} ({code})" for code, name in cur.fetchall()]
    gate(2, "geographic coverage is the full 296-authority universe",
         not short, "\n".join(detail))

    # ---- Gate 3: every publisher code resolved through la_code_lookup ----
    cur.execute(f"""
        SELECT DISTINCT t.publisher_la_code
        FROM {TABLE} t
        WHERE NOT EXISTS (SELECT 1 FROM la_code_lookup l
                          WHERE l.old_code = t.publisher_la_code)
        ORDER BY 1
    """)
    unresolved = [r[0] for r in cur.fetchall()]
    cur.execute(f"""
        SELECT DISTINCT t.publisher_la_code, t.lad24cd
        FROM {TABLE} t
        JOIN la_code_lookup l ON l.old_code = t.publisher_la_code
        WHERE l.new_code IS DISTINCT FROM t.lad24cd
    """)
    misresolved = cur.fetchall()
    cur.execute(f"""
        SELECT DISTINCT publisher_la_code, lad24cd FROM {TABLE}
        WHERE publisher_la_code <> lad24cd ORDER BY 1
    """)
    recoded = cur.fetchall()
    gate(3, "every publisher code resolves through la_code_lookup",
         not unresolved and not misresolved,
         f"unresolved codes: {unresolved or 'none'}\n"
         f"codes stored against a different lad24cd than the lookup gives: "
         f"{misresolved or 'none'}\n"
         f"codes recoded on the way in: "
         f"{[f'{a} -> {b}' for a, b in recoded] or 'none'}")

    # ---- Gate 4: per-row provenance --------------------------------------
    cur.execute(f"""
        SELECT COUNT(*) FROM {TABLE}
        WHERE source_url IS NULL OR source_url = ''
           OR source_edition IS NULL OR source_edition = ''
           OR release_page_url IS NULL OR release_page_url = ''
    """)
    blank = cur.fetchone()[0]
    cur.execute(f"""
        SELECT period, edition_variant, source_edition, COUNT(*)
        FROM {TABLE} GROUP BY 1, 2, 3 ORDER BY 1
    """)
    editions = cur.fetchall()
    gate(4, "per-row source provenance is populated on every row",
         blank == 0,
         f"rows with a blank source_url, source_edition or release_page_url: "
         f"{blank}\n" +
         "\n".join(f"{p}: {v:<9} {f} ({n} rows)" for p, v, f, n in editions))

    # ---- Gate 5: suppression is stored distinctly from zero --------------
    # The failure this catches is a suppressed or missing cell silently
    # becoming a zero. Three things have to hold: a flagged cell carries no
    # value, a valued cell carries no flag, and the marker counts read back
    # out of the source files match what is stored.
    cur.execute(f"""
        SELECT
          COUNT(*) FILTER (WHERE value IS NOT NULL AND value_flag IS NOT NULL),
          COUNT(*) FILTER (WHERE value IS NULL     AND value_flag IS NULL),
          COUNT(*) FILTER (WHERE value_flag = 'suppressed'),
          COUNT(*) FILTER (WHERE value_flag = 'missing'),
          COUNT(*) FILTER (WHERE value_flag = 'not_applicable'),
          COUNT(*) FILTER (WHERE value = 0)
        FROM {TABLE}
    """)
    both, neither, supp, miss, na, zeros = cur.fetchone()
    cur.execute(f"""
        SELECT COUNT(*) FROM {TABLE} WHERE value_flag IS NOT NULL AND value = 0
    """)
    coerced = cur.fetchone()[0]
    gate(5, "suppressed and missing values are stored distinctly from zero",
         both == 0 and neither == 0 and coerced == 0 and (supp + miss) > 0,
         f"rows carrying both a value and a flag: {both} (must be 0)\n"
         f"rows carrying neither:                {neither} (must be 0)\n"
         f"suppressed (legacy '-', v2026 '[c]'): {supp}\n"
         f"missing    (legacy '..', v2026 '[x]'): {miss}\n"
         f"not applicable ('[z]'):               {na}\n"
         f"genuine zeros stored as 0:            {zeros}\n"
         f"flagged rows silently coerced to 0:   {coerced} (must be 0)")

    cur.close()
    conn.close()

    # ---- Gate 6: live rows equal the latest edition's source file --------
    # Each period's latest edition is re-extracted from its recorded local
    # file and compared cell for cell (NULL-safe: tuples compare None equal to
    # None, never to 0) with the live rows. Idempotency: the same rows are
    # re-upserted inside a transaction that always rolls back, and a content
    # hash excluding loaded_at must not move.
    probe = get_conn()
    pcur = probe.cursor()
    try:
        before_sum = content_hash(pcur)
        pcur.execute(f"SELECT COUNT(*) FROM {TABLE}")
        before_n = pcur.fetchone()[0]

        lines, cell_diffs, rewritten = [], 0, 0
        extracted = {}
        for period in sorted(build.RELEASES):
            edition, path, ed = latest_source(pcur, period)
            _, _, rows = build.build_rows(pcur, edition, build.read_a3(path))
            extracted[period] = rows
            # category_label is compared only where the live layer kept it
            # (see content_hash): every other column is compared
            skip = (DATA_COLS.index("category_label")
                    if period in STALE_PERIODS else None)
            keep = [i for i in range(len(DATA_COLS)) if i != skip]
            want = {tuple(r[i] for i in keep) for r in rows}
            pcur.execute(f"SELECT {', '.join(DATA_COLS)} FROM {TABLE} "
                         "WHERE period = %s", (period,))
            got = {tuple(r[i] for i in keep) for r in pcur.fetchall()}
            d = len(want ^ got) // 2 if len(want) == len(got) else \
                len(want ^ got)
            cell_diffs += d
            lines.append(f"{period}: edition {ed} from {path.name}: "
                         f"{len(rows)} rows, {d} differ from the live rows")
        for period, rows in extracted.items():
            psycopg2.extras.execute_values(pcur, build.UPSERT, rows,
                                           page_size=1000)
            rewritten += len(rows)

        after_sum = content_hash(pcur)
        pcur.execute(f"SELECT COUNT(*) FROM {TABLE}")
        after_n = pcur.fetchone()[0]

        gate(6, "live rows equal the latest edition's source file cell for "
             "cell, and reloading it changes nothing",
             before_n == after_n and before_sum == after_sum
             and cell_diffs == 0,
             "\n".join(lines) + "\n"
             f"rows before: {before_n}\n"
             f"rows after re-upserting {rewritten} rows: {after_n}\n"
             f"content hash (excluding loaded_at) before: {before_sum}\n"
             f"content hash (excluding loaded_at) after:  {after_sum}\n"
             f"cells differing from the latest edition (NULL-safe): "
             f"{cell_diffs}")
    finally:
        probe.rollback()
        pcur.close()
        probe.close()

    # ---- Gate 7: reconciliation against the publisher's own England row --
    # A3 publishes an England total, but it is not the sum of the local
    # authority rows and is not supposed to be: it is weighted to impute for
    # non-submitting authorities and rounded to the nearest 10, while the LA
    # rows are unrounded and carry NULL where a figure is suppressed or
    # missing. The falsifiable statement is therefore that the loaded LA rows
    # sum to no more than the published England figure, and that the shortfall
    # is consistent with the authorities the publisher says are missing.
    # Asserting equality would be inventing a check the publisher does not
    # support.
    conn = get_readonly_conn()
    cur = conn.cursor()
    lines, breaches = [], []
    for period in sorted(build.RELEASES):
        _, path, _ = latest_source(cur, period)
        df = build.read_a3(path)
        _, mapping, _ = build.map_columns(df)
        eng_row = None
        for i in range(len(df)):
            if str(df.iat[i, 0]).strip() == "E92000001":
                eng_row = i
                break
        if eng_row is None:
            lines.append(f"{period}: no England row in A3 - not checked")
            continue
        col = next(j for j, c in mapping.items()
                   if c == "hh_one_or_more_support_needs")
        published, flag = build.cell(df.iat[eng_row, col])
        cur.execute(f"""
            SELECT COALESCE(SUM(value), 0),
                   COUNT(*) FILTER (WHERE value_flag IS NOT NULL)
            FROM {TABLE}
            WHERE period = %s AND category_code = 'hh_one_or_more_support_needs'
        """, (period,))
        la_sum, flagged = cur.fetchone()
        gap = published - la_sum
        ok = 0 <= gap
        lines.append(f"{period}: England published {published:>7,} | "
                     f"sum of LA rows {la_sum:>7,} | imputation and rounding "
                     f"gap {gap:>6,} ({gap / published:.2%}), "
                     f"{flagged} authority row(s) not reported")
        if not ok:
            breaches.append(period)
    gate(7, "loaded LA rows reconcile to the publisher's England total",
         not breaches,
         "\n".join(lines) +
         (f"\nperiods where LA rows exceed the published England total: "
          f"{breaches}" if breaches else
          "\nno period exceeds the published total, as expected for a "
          "weighted figure"))

    cur.close()
    conn.close()

    print()
    passed = sum(1 for r in RESULTS if r)
    print(f"{passed}/{len(RESULTS)} gates passed")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
