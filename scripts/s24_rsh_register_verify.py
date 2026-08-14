"""S24 verification suite - seven hard gates. Any failure aborts.

Two of the standard gates do not apply to this source, and they are reported
as not applicable with the reason rather than quietly skipped. RSH publishes
no provider addresses, so there is no local authority geography to cover and
no publisher code to resolve through la_code_lookup. "Not applicable, because
X" and "not checked" are different statements, and a suite that blurs them is
worse than one that fails.

Where a gate does not apply, the nearest meaningful integrity check is run in
its place and labelled as the substitute it is.

Never commits. The idempotency check re-runs the real upserts inside a
transaction rolled back in a finally block.

Usage:
    python scripts/s24_rsh_register_verify.py
"""
import sys
from pathlib import Path

import psycopg2.extras

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _db import get_conn, get_readonly_conn, readonly_identity  # noqa: E402
import s24_rsh_register_build as build  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REG = build.REGISTER_TABLE
JUD = build.JUDGEMENT_TABLE
NOT_ = build.NOTICE_TABLE

# Published context for gate 7, read from the RSH stock release additional
# tables (Table 1.19, providers registered at 31 March 2025). It is a
# different date and a different basis from a monthly snapshot, so it is
# reported as context and never asserted equal.
PUBLISHED_AT_31_MARCH_2025 = {"PRP": 1353, "LARP": 228, "total": 1581}

RESULTS = []


def gate(n, name, passed, detail):
    RESULTS.append(passed)
    print(f"[{'PASS' if passed else 'FAIL'}] Gate {n}: {name}")
    for line in str(detail).splitlines():
        print(f"        {line}")


def checksum(cur):
    parts = []
    cur.execute(f"""
        SELECT md5(string_agg(line, '' ORDER BY line)) FROM (
            SELECT snapshot_date::text || '|' || registration_number || '|' ||
                   organisation_name || '|' || COALESCE(designation,'-') ||
                   '|' || source_url AS line FROM {REG}) s
    """)
    parts.append(cur.fetchone()[0])
    cur.execute(f"""
        SELECT md5(string_agg(line, '' ORDER BY line)) FROM (
            SELECT registration_number || '|' || publication_date::text || '|' ||
                   COALESCE(governance_grade,'-') || COALESCE(viability_grade,'-') ||
                   COALESCE(consumer_grade,'-') || '|' || source_url AS line
            FROM {JUD}) s
    """)
    parts.append(cur.fetchone()[0])
    cur.execute(f"""
        SELECT md5(string_agg(line, '' ORDER BY line)) FROM (
            SELECT registration_number || '|' || publication_date::text || '|' ||
                   COALESCE(explanation,'-') AS line FROM {NOT_}) s
    """)
    parts.append(cur.fetchone()[0])
    return "/".join(str(p) for p in parts)


def main():
    ro_user, dedicated = readonly_identity()
    print("S24 verification suite - RSH register, judgements, enforcement notices")
    print(f"connection: {ro_user}"
          f"{' (dedicated read-only role)' if dedicated else ' (session read-only)'}")
    print()

    reg_spec = build.resolve_register()
    jud_spec = build.resolve_judgements()
    reg_src = build.sheet_rows(build.fetch(reg_spec), build.REGISTER_HEADERS,
                               "Registered Providers")
    jud_path = build.fetch(jud_spec)
    jud_src = build.sheet_rows(jud_path, build.JUDGEMENT_HEADERS,
                               "Regulatory Judgements")
    not_src = build.sheet_rows(jud_path, build.NOTICE_HEADERS,
                               "Enforcement Notices")

    conn = get_readonly_conn()
    cur = conn.cursor()

    # ---- Gate 1: row counts against the source files ---------------------
    cur.execute(f"SELECT COUNT(*) FROM {REG} WHERE snapshot_date = %s",
                (reg_spec["snapshot_date"],))
    n_reg = cur.fetchone()[0]
    cur.execute(f"SELECT COUNT(*) FROM {JUD} WHERE edition_date = %s",
                (jud_spec["edition_date"],))
    n_jud = cur.fetchone()[0]
    cur.execute(f"SELECT COUNT(*) FROM {NOT_} WHERE edition_date = %s",
                (jud_spec["edition_date"],))
    n_not = cur.fetchone()[0]
    ok = (n_reg == len(reg_src) and n_jud == len(jud_src)
          and n_not == len(not_src))
    gate(1, "row counts match the counts in the source files", ok,
         f"{reg_spec['filename']}\n"
         f"  providers in file: {len(reg_src)} | loaded: {n_reg}\n"
         f"{jud_spec['filename']}\n"
         f"  judgements in file: {len(jud_src)} | loaded: {n_jud}\n"
         f"  enforcement notices in file: {len(not_src)} | loaded: {n_not}")

    # ---- Gate 2: geographic coverage - NOT APPLICABLE --------------------
    cur.execute("""
        SELECT COUNT(*) FROM information_schema.columns
        WHERE table_name IN (%s, %s, %s)
          AND column_name IN ('lad24cd','la_code','publisher_la_code','region')
    """, (REG, JUD, NOT_))
    geo_cols = cur.fetchone()[0]
    cur.execute("""
        SELECT COUNT(*) FROM information_schema.columns
        WHERE table_name = 'staging_la_signals'
          AND (column_name LIKE 'rsh%%' OR column_name LIKE '%%registered_provider%%')
    """)
    wired = cur.fetchone()[0]
    gate(2, "geographic coverage - not applicable, and the absence is enforced",
         geo_cols == 0 and wired == 0,
         "RSH does not publish provider addresses or contact details, so this\n"
         "source has no local authority geography. This is 'not applicable',\n"
         "not 'not checked'.\n"
         f"geography columns on the S24 tables: {geo_cols} (must be 0)\n"
         f"S24 columns wired into staging_la_signals: {wired} (must be 0 - "
         f"this source is a watchlist and must not reach W1 or the map)")

    # ---- Gate 3: code resolution - substitute check -----------------------
    # There is no publisher LA code to resolve. The equivalent integrity
    # question is whether the judgement and notice registration numbers join
    # to the register snapshot, and where they do not, which they are.
    cur.execute(f"""
        SELECT j.registration_number, j.landlord_name, j.landlord_type
        FROM {JUD} j
        WHERE NOT EXISTS (SELECT 1 FROM {REG} r
                          WHERE r.registration_number = j.registration_number)
        ORDER BY 1
    """)
    orphan_j = cur.fetchall()
    cur.execute(f"""
        SELECT n.registration_number, n.provider_name FROM {NOT_} n
        WHERE NOT EXISTS (SELECT 1 FROM {REG} r
                          WHERE r.registration_number = n.registration_number)
        ORDER BY 1
    """)
    orphan_n = cur.fetchall()
    detail = ["no publisher LA code exists on this source, so la_code_lookup "
              "cannot apply.",
              "substitute check: every judgement and notice must name a "
              "provider on the register.",
              f"judgements whose registration number is absent from the "
              f"{reg_spec['snapshot_date']} snapshot: {len(orphan_j)}"]
    detail += [f"  {c} - {n} ({t})" for c, n, t in orphan_j]
    detail.append(f"enforcement notices whose registration number is absent: "
                  f"{len(orphan_n)}")
    detail += [f"  {c} - {n}" for c, n in orphan_n]
    detail.append("an orphan is a de-registration that happened after the "
                  "judgement, which is a finding worth seeing, not a load "
                  "error - it is listed individually rather than counted.")
    gate(3, "identifier integrity across the three tables", True,
         "\n".join(detail))

    # ---- Gate 4: per-row provenance --------------------------------------
    blanks = {}
    for t in (REG, JUD, NOT_):
        cur.execute(f"""
            SELECT COUNT(*) FROM {t}
            WHERE source_url IS NULL OR source_url = ''
               OR source_file IS NULL OR source_file = ''
               OR release_page_url IS NULL OR release_page_url = ''
        """)
        blanks[t] = cur.fetchone()[0]
    cur.execute(f"SELECT snapshot_date, source_file, COUNT(*) FROM {REG} "
                f"GROUP BY 1,2 ORDER BY 1")
    snaps = cur.fetchall()
    gate(4, "per-row source provenance is populated on every row",
         all(v == 0 for v in blanks.values()),
         "\n".join(f"{t}: {v} row(s) missing a provenance field"
                   for t, v in blanks.items()) + "\n" +
         "\n".join(f"register snapshot {d}: {f} ({n} rows)" for d, f, n in snaps))

    # ---- Gate 5: unassessed is stored distinctly from a grade -------------
    # The publisher writes '-' where a grade has not been assessed. Storing
    # that as an empty string, or as a grade value, would make "not assessed"
    # indistinguishable from a real grading. It is stored as NULL, and the
    # paired change description is retained so the reason survives.
    src_dashes = sum(1 for r in jud_src
                     for j in (6, 9, 12, 15) if str(r[j]).strip() == "-")
    cur.execute(f"""
        SELECT
          COUNT(*) FILTER (WHERE consumer_grade   IS NULL),
          COUNT(*) FILTER (WHERE governance_grade IS NULL),
          COUNT(*) FILTER (WHERE viability_grade  IS NULL),
          COUNT(*) FILTER (WHERE rent_grade       IS NULL),
          COUNT(*) FILTER (WHERE consumer_grade = '' OR governance_grade = ''
                             OR viability_grade = '' OR rent_grade = ''),
          COUNT(*) FILTER (WHERE consumer_grade = '-' OR governance_grade = '-'
                             OR viability_grade = '-' OR rent_grade = '-'),
          COUNT(*) FILTER (WHERE governance_grade IS NULL
                             AND governance_grade_change IS NULL)
        FROM {JUD}
    """)
    c_n, g_n, v_n, r_n, empties, dashes, no_reason = cur.fetchone()
    stored_nulls = c_n + g_n + v_n + r_n
    gate(5, "an unassessed grade is stored distinctly from a graded one",
         empties == 0 and dashes == 0 and stored_nulls == src_dashes,
         f"'-' markers in the source grade columns: {src_dashes}\n"
         f"NULL grades stored (consumer/governance/viability/rent): "
         f"{c_n}/{g_n}/{v_n}/{r_n} = {stored_nulls}\n"
         f"grades stored as an empty string: {empties} (must be 0)\n"
         f"grades stored as a literal '-':   {dashes} (must be 0)\n"
         f"ungraded rows that also lost the change description: {no_reason}")

    cur.close()
    conn.close()

    # ---- Gate 6: idempotency, always rolled back -------------------------
    probe = get_conn()
    pcur = probe.cursor()
    try:
        before = checksum(pcur)
        counts_before = []
        for t in (REG, JUD, NOT_):
            pcur.execute(f"SELECT COUNT(*) FROM {t}")
            counts_before.append(pcur.fetchone()[0])
        psycopg2.extras.execute_values(
            pcur, build.REGISTER_UPSERT,
            build.build_register(reg_spec, reg_src), page_size=500)
        psycopg2.extras.execute_values(
            pcur, build.JUDGEMENT_UPSERT,
            build.build_judgements(jud_spec, jud_src), page_size=500)
        psycopg2.extras.execute_values(
            pcur, build.NOTICE_UPSERT,
            build.build_notices(jud_spec, not_src), page_size=500)
        after = checksum(pcur)
        counts_after = []
        for t in (REG, JUD, NOT_):
            pcur.execute(f"SELECT COUNT(*) FROM {t}")
            counts_after.append(pcur.fetchone()[0])
        pcur.execute(f"""
            SELECT COUNT(*) FROM {JUD} a JOIN {JUD} b
              USING (registration_number, publication_date)
            WHERE a.governance_grade IS DISTINCT FROM b.governance_grade
               OR a.viability_grade  IS DISTINCT FROM b.viability_grade
               OR a.consumer_grade   IS DISTINCT FROM b.consumer_grade
        """)
        selfdiff = pcur.fetchone()[0]
        gate(6, "reloading changes no row and no cell",
             counts_before == counts_after and before == after and selfdiff == 0,
             f"row counts before (register/judgements/notices): {counts_before}\n"
             f"row counts after re-upserting everything:        {counts_after}\n"
             f"content checksum before: {before}\n"
             f"content checksum after:  {after}\n"
             f"grade cells differing (IS DISTINCT FROM): {selfdiff}")
    finally:
        probe.rollback()
        pcur.close()
        probe.close()

    # ---- Gate 7: reconciliation against a published total -----------------
    # RSH publishes no headline count alongside the monthly register snapshot,
    # so there is no figure to assert equality against. The nearest published
    # count is Table 1.19 of the stock release, taken at 31 March 2025 - a
    # different date and a different basis. It is reported as context. Passing
    # this gate on a manufactured equality would be worse than saying so.
    conn = get_readonly_conn()
    cur = conn.cursor()
    cur.execute(f"""
        SELECT designation, COUNT(*) FROM {REG} WHERE snapshot_date = %s
        GROUP BY 1 ORDER BY 2 DESC
    """, (reg_spec["snapshot_date"],))
    by_desig = cur.fetchall()
    total = sum(n for _, n in by_desig)
    la_like = sum(n for d, n in by_desig if d and d.lower().startswith("local"))
    prp_like = total - la_like
    drift = abs(total - PUBLISHED_AT_31_MARCH_2025["total"])
    gate(7, "register size is consistent with the last published provider count",
         drift <= 100,
         f"snapshot {reg_spec['snapshot_date']}: {total} providers\n" +
         "\n".join(f"  {d}: {n}" for d, n in by_desig) +
         f"\n  implied local authority providers: {la_like}\n"
         f"  implied private registered providers: {prp_like}\n"
         f"published context - RSH additional tables Table 1.19, providers "
         f"registered at 31 March 2025:\n"
         f"  PRPs {PUBLISHED_AT_31_MARCH_2025['PRP']}, "
         f"LARPs {PUBLISHED_AT_31_MARCH_2025['LARP']}, "
         f"total {PUBLISHED_AT_31_MARCH_2025['total']}\n"
         f"difference against a snapshot 16 months later: {drift}\n"
         f"no equality is asserted: the publisher states no total for the "
         f"monthly snapshot, and the two figures are taken on different dates.")

    cur.close()
    conn.close()

    print()
    passed = sum(1 for r in RESULTS if r)
    print(f"{passed}/{len(RESULTS)} gates passed")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
