"""Apply ten verified corrections to la_code_lookup.

Found by a full audit of all 333 rows on 2026-07-26. Four separate
reorganisations carried defects: Cumbria (fixed earlier), Northamptonshire,
Suffolk and Buckinghamshire.

Every mapping below was verified against the ONS area page for the successor
where ONS publishes one, and against GOV.UK / council / findthatpostcode
sources for the 2019 and 2021 changes where it does not.

Parameterised SQL, single transaction, before/after reported.
"""

import os
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
load_dotenv(_HERE.parent / ".env")
load_dotenv(_HERE.parent.parent / ".env")


def _require_env(name):
    value = os.environ.get(name)
    if not value:
        sys.exit(f"HARD STOP: {name} is not set. Set it in the environment "
                 f"or .env. This script will not guess a credential.")
    return value


DB_CFG = dict(
    # PG_HOST is the Docker service name inside the compose network; from the
    # Windows host the same cluster is reached on localhost.
    host=(os.environ.get("PG_HOST") or "localhost").replace("postgres",
                                                            "localhost"),
    port=int(os.environ.get("PG_PORT", "5432")),
    dbname=os.environ.get("PG_DATABASE", "exempt_pipeline"),
    user=_require_env("PG_USER"),
    password=_require_env("PG_PASSWORD"),
)

# (old_code, new_code, la_name, change_type, effective_date, note)
CORRECTIONS = [
    ("E07000150", "E06000061", "North Northamptonshire", "new_unitary",
     "2021-04-01", "Corby -> North Northamptonshire. Was E06000062; "
     "transposed with Daventry."),
    ("E07000151", "E06000062", "West Northamptonshire", "new_unitary",
     "2021-04-01", "Daventry -> West Northamptonshire. Was E06000061; "
     "transposed with Corby."),
    ("E07000201", "E07000245", "West Suffolk", "merger",
     "2019-04-01", "Forest Heath -> West Suffolk. Was E07000244 East "
     "Suffolk; Forest Heath merged with St Edmundsbury to form West Suffolk."),
    ("E07000004", "E06000060", "Buckinghamshire", "new_unitary",
     "2020-04-01", "Aylesbury Vale -> Buckinghamshire. Was E07000245 West "
     "Suffolk; a Buckinghamshire district misfiled under Suffolk."),
    ("E07000005", "E06000060", "Buckinghamshire", "new_unitary",
     "2020-04-01", "Chiltern -> Buckinghamshire. Was E07000245 West "
     "Suffolk; a Buckinghamshire district misfiled under Suffolk."),
]

INSERTIONS = [
    ("E07000204", "E07000245", "West Suffolk", "merger",
     "2019-04-01", "St Edmundsbury -> West Suffolk. Was absent."),
    ("E07000206", "E07000244", "East Suffolk", "merger",
     "2019-04-01", "Waveney -> East Suffolk. Was absent."),
    ("E07000006", "E06000060", "Buckinghamshire", "new_unitary",
     "2020-04-01", "South Bucks -> Buckinghamshire. Was absent."),
    ("E07000007", "E06000060", "Buckinghamshire", "new_unitary",
     "2020-04-01", "Wycombe -> Buckinghamshire. Was absent."),
    ("E10000002", "E06000060", "Buckinghamshire", "new_unitary",
     "2020-04-01", "Buckinghamshire county council -> Buckinghamshire "
     "unitary. Was absent."),
]

COL_COMMENTS = """
COMMENT ON COLUMN la_code_lookup.old_code IS
  'Code as published by a SOURCE. Not necessarily chronologically older than '
  'new_code. Example: E08000038 (Barnsley, created 2025) appears here mapping '
  'to E08000016 (Barnsley, the LAD24 code this pipeline keys on), because a '
  '2025 source publishes the newer code and the pipeline stores the LAD24 '
  'vintage. Direction is source-to-pipeline, not old-to-new.';

COMMENT ON COLUMN la_code_lookup.new_code IS
  'Code used by THIS PIPELINE: a live LAD24CD present in la_boundaries. '
  'Every lookup resolves a published code to this. See the comment on '
  'old_code: the pair is source-to-pipeline, not chronological.';

COMMENT ON TABLE la_code_lookup IS
  'Resolves any local authority code a source might publish to the LAD24CD '
  'this pipeline keys on. 296 identity rows (one per live LAD) plus mappings '
  'for abolished districts and recodes. Fully audited 2026-07-26 against ONS '
  'area pages; see docs/decisions/2026-07-26-la-code-lookup-full-audit.md.';
"""


def main():
    conn = psycopg2.connect(**DB_CFG)
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    try:
        codes = [c[0] for c in CORRECTIONS + INSERTIONS]
        cur.execute("SELECT old_code, new_code, la_name FROM la_code_lookup "
                    "WHERE old_code = ANY(%s)", (codes,))
        before = {r["old_code"]: (r["new_code"], r["la_name"])
                  for r in cur.fetchall()}

        print("=" * 78)
        print("BEFORE")
        print("=" * 78)
        for code in codes:
            b = before.get(code)
            print(f"  {code}  {b[0] + '  ' + b[1] if b else 'ABSENT'}")

        for old, new, name, ctype, eff, note in CORRECTIONS:
            cur.execute("""
                UPDATE la_code_lookup
                   SET new_code = %s, la_name = %s, change_type = %s,
                       effective_date = %s, notes = %s, loaded_at = now()
                 WHERE old_code = %s
            """, (new, name, ctype, eff, note, old))
            if cur.rowcount != 1:
                raise RuntimeError(f"{old}: expected 1 row, got {cur.rowcount}")

        for old, new, name, ctype, eff, note in INSERTIONS:
            cur.execute("""
                INSERT INTO la_code_lookup
                    (old_code, new_code, la_name, change_type, effective_date,
                     notes, loaded_at)
                VALUES (%s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (old_code) DO UPDATE
                   SET new_code = EXCLUDED.new_code,
                       la_name = EXCLUDED.la_name,
                       change_type = EXCLUDED.change_type,
                       effective_date = EXCLUDED.effective_date,
                       notes = EXCLUDED.notes,
                       loaded_at = now()
            """, (old, new, name, ctype, eff, note))

        cur.execute(COL_COMMENTS)

        cur.execute("SELECT old_code, new_code, la_name FROM la_code_lookup "
                    "WHERE old_code = ANY(%s) ORDER BY old_code", (codes,))
        print("\n" + "=" * 78)
        print("AFTER")
        print("=" * 78)
        for r in cur.fetchall():
            print(f"  {r['old_code']}  {r['new_code']}  {r['la_name']}")

        # Integrity re-check before committing.
        cur.execute("SELECT count(*) FROM la_code_lookup")
        total = cur.fetchone()[0]
        cur.execute("""
            SELECT count(*) FROM la_code_lookup l
             WHERE NOT EXISTS (SELECT 1 FROM la_boundaries b
                                WHERE b.lad24cd = l.new_code)
        """)
        dangling = cur.fetchone()[0]
        cur.execute("""
            SELECT old_code FROM la_code_lookup
             GROUP BY old_code HAVING count(*) > 1
        """)
        dupes = [r[0] for r in cur.fetchall()]
        print(f"\n  total rows            : {total} (was 333, +5 insertions)")
        print(f"  targets not in boundaries: {dangling} (must be 0)")
        print(f"  duplicate old_code    : {dupes or 'none'}")
        if dangling or dupes or total != 338:
            raise RuntimeError("integrity check failed; rolling back")

        conn.commit()
        print("\nCommitted.")
    except Exception:
        conn.rollback()
        print("\nROLLED BACK.")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
