"""Correct the 2023 local government reorganisation mappings in la_code_lookup.

Defect (found during the S6 asylum support build):
  1. E07000027 Barrow-in-Furness was mapped to E06000063 Cumberland.
     It belongs to E06000064 Westmorland and Furness.
  2. E07000028 Carlisle was absent entirely (should map to E06000063 Cumberland).
  3. E07000189 South Somerset was absent entirely (should map to E06000066 Somerset).

Root cause: an off-by-one transcription when the Cumberland predecessors were
populated — E07000026/E07000027/E07000029 was typed where
E07000026/E07000028/E07000029 was meant. The single slip both created the wrong
Barrow mapping and left Carlisle absent.

Authority:
  ONS explore-local-statistics area pages for E06000063, E06000064 and E06000066.
  The Cumbria (Structural Changes) Order 2022 and The Somerset (Structural
  Changes) Order 2022, legislation.gov.uk.

Scope: touches exactly three rows of la_code_lookup. No other row is modified.
Idempotent — safe to re-run.
"""

import os
import sys

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv(r"C:\Users\slewi\ucws-repo\.env")

DB_HOST = (os.getenv("PG_HOST") or "localhost").replace("postgres", "localhost")
DB_CFG = dict(
    host=DB_HOST,
    port=int(os.getenv("PG_PORT", "5432")),
    dbname=os.getenv("PG_DATABASE", "exempt_pipeline"),
    user=os.getenv("PG_USER", "n8nuser"),
    password=os.getenv("PG_PASSWORD", ""),
)

EFFECTIVE_DATE = "2023-04-01"
SOURCE_NOTE = (
    "corrected 2026-07-25: ONS area page + "
    "Cumbria/Somerset (Structural Changes) Order 2022"
)

# (old_code, new_code, la_name, change_type, effective_date, notes)
CORRECTION = (
    "E07000027", "E06000064", "Westmorland and Furness", "new_unitary",
    EFFECTIVE_DATE,
    f"Barrow-in-Furness \u2192 Westmorland and Furness; {SOURCE_NOTE}",
)

INSERTS = [
    ("E07000028", "E06000063", "Cumberland", "new_unitary", EFFECTIVE_DATE,
     f"Carlisle \u2192 Cumberland; {SOURCE_NOTE}"),
    ("E07000189", "E06000066", "Somerset", "new_unitary", EFFECTIVE_DATE,
     f"South Somerset \u2192 Somerset; {SOURCE_NOTE}"),
]

TOUCHED = ("E07000027", "E07000028", "E07000189")


def show(cur, label):
    print(f"\n--- {label}")
    cur.execute(
        """SELECT old_code, new_code, la_name, change_type,
                  effective_date::text, notes
           FROM la_code_lookup WHERE old_code IN %s ORDER BY old_code""",
        (TOUCHED,),
    )
    rows = cur.fetchall()
    if not rows:
        print("    (no rows)")
    for r in rows:
        print(f"    {r[0]} -> {r[1]}  {r[2]:<26} {r[3]:<12} {r[4]}")
        print(f"        {r[5]}")
    missing = set(TOUCHED) - {r[0] for r in rows}
    if missing:
        print(f"    MISSING: {sorted(missing)}")


def main():
    conn = psycopg2.connect(**DB_CFG)
    conn.autocommit = False
    cur = conn.cursor()
    try:
        cur.execute("SELECT count(*) FROM la_code_lookup")
        before_total = cur.fetchone()[0]
        show(cur, f"BEFORE (table has {before_total} rows)")

        # Guard: only proceed if the row is in the state we diagnosed, or already fixed.
        cur.execute(
            "SELECT new_code FROM la_code_lookup WHERE old_code = %s", ("E07000027",)
        )
        row = cur.fetchone()
        if row is None:
            sys.exit("HARD STOP: E07000027 not present — table is not in the expected state.")
        if row[0] not in ("E06000063", "E06000064"):
            sys.exit(f"HARD STOP: E07000027 points at unexpected {row[0]} — aborting.")

        # 1. Correct Barrow-in-Furness.
        cur.execute(
            """UPDATE la_code_lookup
                  SET new_code = %s, la_name = %s, change_type = %s,
                      effective_date = %s, notes = %s, loaded_at = now()
                WHERE old_code = %s""",
            (CORRECTION[1], CORRECTION[2], CORRECTION[3],
             CORRECTION[4], CORRECTION[5], CORRECTION[0]),
        )
        n_updated = cur.rowcount

        # 2. Insert Carlisle and South Somerset.
        psycopg2.extras.execute_batch(
            cur,
            """INSERT INTO la_code_lookup
                   (old_code, new_code, la_name, change_type,
                    effective_date, notes, loaded_at)
               VALUES (%s, %s, %s, %s, %s, %s, now())
               ON CONFLICT (old_code) DO UPDATE
                   SET new_code       = EXCLUDED.new_code,
                       la_name        = EXCLUDED.la_name,
                       change_type    = EXCLUDED.change_type,
                       effective_date = EXCLUDED.effective_date,
                       notes          = EXCLUDED.notes,
                       loaded_at      = now()""",
            INSERTS,
        )

        cur.execute("SELECT count(*) FROM la_code_lookup")
        after_total = cur.fetchone()[0]

        # Post-conditions before commit.
        cur.execute(
            """SELECT old_code, new_code FROM la_code_lookup
               WHERE old_code IN %s ORDER BY old_code""", (TOUCHED,))
        got = dict(cur.fetchall())
        expected = {"E07000027": "E06000064",
                    "E07000028": "E06000063",
                    "E07000189": "E06000066"}
        assert got == expected, f"post-condition failed: {got}"

        # The three new-unitary families must now match ONS exactly.
        cur.execute(
            """SELECT new_code, array_agg(old_code ORDER BY old_code)
               FROM la_code_lookup
               WHERE change_type = 'new_unitary'
                 AND new_code IN ('E06000063','E06000064','E06000066')
               GROUP BY 1 ORDER BY 1""")
        families = dict(cur.fetchall())
        assert families["E06000063"] == ["E07000026", "E07000028", "E07000029"], \
            f"Cumberland family wrong: {families['E06000063']}"
        assert families["E06000064"] == ["E07000027", "E07000030", "E07000031"], \
            f"Westmorland and Furness family wrong: {families['E06000064']}"

        print(f"\nUPDATE affected {n_updated} row(s); "
              f"row count {before_total} -> {after_total} (+{after_total - before_total})")
        show(cur, "AFTER (uncommitted)")

        conn.commit()
        print("\nCOMMITTED.")

        print("\n--- Verification: predecessor families now on record")
        for code, name in [("E06000063", "Cumberland"),
                           ("E06000064", "Westmorland and Furness"),
                           ("E06000066", "Somerset")]:
            print(f"    {name:26} <- {', '.join(families[code])}")

    except Exception:
        conn.rollback()
        print("\nROLLED BACK — no changes made.")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
