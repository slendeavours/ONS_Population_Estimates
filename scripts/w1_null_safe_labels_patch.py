"""Make every W1 label branch explicitly on NULL before anything else.

The design defect
-----------------
The code split that dropped Barnsley and Sheffield out of W1 was a data
defect affecting two authorities. This is the design defect underneath it,
and it affects every authority where any signal is ever absent, from any
cause.

`ta_trend_label` ended with a catch-all:

    CASE
        WHEN ta_cur.households_in_ta = 0 THEN 'submission_gap'
        WHEN ta_prev.households_in_ta IS NULL OR ... THEN 'no_prior_year'
        WHEN ... > 10 THEN 'rising_strongly'
        ...
        ELSE 'falling_strongly'
    END

`NULL = 0` is NULL, not true, so a missing row never reaches
`submission_gap`. Every arithmetic comparison against a NULL current figure is
also NULL. All of them fall through, and the ELSE publishes an absent
measurement as the strongest available downward signal.

The pipeline already had the right label and it never fired. An absent
measurement published as `falling_strongly` is worse than publishing nothing,
because it is confidently wrong in a specific direction.

The same construction sat in the data quality object, where it did more
damage for its size:

    'ta_current', CASE WHEN ta_cur.households_in_ta = 0
                       THEN 'submission_gap' ELSE 'ok' END

A missing row was reported as **'ok'**. The monitoring layer affirmatively
said the data was fine. That is the failure the source registry names
elsewhere - a monitor that manufactures confidence is worse than no monitor.

The rule being encoded
----------------------
Absent, zero and suppressed are three different states and the pipeline must
never collapse them.

  absent      no row joined            -> 'no_current_data' / 'missing'
  zero        a row saying zero        -> 'submission_gap'
  suppressed  publisher withheld it    -> carried on the source table's flag

Two structural changes, applied to both label CASEs:

  1. The NULL test comes first, before any comparison can swallow it.
  2. The terminal ELSE is 'undetermined', not a real label. The last real
     branch now states its own condition (<= -10). An unforeseen combination
     produces a value that reads as a gap, never as a direction.

Audit scope
-----------
All 9 nodes of Workflow 1 were read from n8ndb. Twelve CASE expressions
exist, in two nodes:

  National Aggregates (5)  SUM(CASE WHEN period = ... ) pivot idioms. Not
                           label producers. Left alone, but note that a
                           missing authority silently reduces the national
                           total rather than flagging it.
  LA Signals (7)           ta_trend_label            DEFECTIVE, fixed here
                           data_quality.ta_current   DEFECTIVE, fixed here
                           data_quality.ta_trend     widened to test current
                           data_quality.rough_sleeping  already NULL-first
                           data_quality.marac           already NULL-first
                           efs_flag, s114_flag       presence flags over
                                                     fully loaded reference
                                                     tables (113 and 15 rows,
                                                     33 and 10 authorities
                                                     true); absence genuinely
                                                     means not listed

Tenant Type Rankings filters `WHERE crfd_days IS NOT NULL` and is already
safe. Section 3, Create Run, Mark Run Complete and the pre-flight carry no
CASE.

Standing rule 1 requires the live node in n8ndb to be updated in the same
session as any change, so this writes there and backs the previous JSON up
first. Editing only the documentation would leave the workflow unable to
reproduce its own output, which is the defect that decision record exists to
prevent.

Usage:
    python scripts/w1_null_safe_labels_patch.py --check
    python scripts/w1_null_safe_labels_patch.py --apply
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _db import ENV, _require  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parent.parent
BACKUP_DIR = REPO / "build_reports"
WORKFLOW_ID = "IrrglXLYcphSg5bC"
NODE = "LA Signals"
DOC = REPO / "docs" / "s22_w1_node5_revised.md"

OLD_TREND = """    CASE
        WHEN ta_cur.households_in_ta = 0 THEN 'submission_gap'
        WHEN ta_prev.households_in_ta IS NULL OR ta_prev.households_in_ta = 0 THEN 'no_prior_year'
        WHEN (ta_cur.households_in_ta - ta_prev.households_in_ta)::NUMERIC
             / NULLIF(ta_prev.households_in_ta, 0) * 100 > 10  THEN 'rising_strongly'
        WHEN (ta_cur.households_in_ta - ta_prev.households_in_ta)::NUMERIC
             / NULLIF(ta_prev.households_in_ta, 0) * 100 > 3   THEN 'rising'
        WHEN (ta_cur.households_in_ta - ta_prev.households_in_ta)::NUMERIC
             / NULLIF(ta_prev.households_in_ta, 0) * 100 > -3  THEN 'flat'
        WHEN (ta_cur.households_in_ta - ta_prev.households_in_ta)::NUMERIC
             / NULLIF(ta_prev.households_in_ta, 0) * 100 > -10 THEN 'falling'
        ELSE 'falling_strongly'
    END AS ta_trend_label,"""

NEW_TREND = """    -- Absent, zero and suppressed are three different states. The NULL test
    -- comes first so no comparison can swallow it, and the terminal ELSE is
    -- 'undetermined' rather than a direction, so an unforeseen combination
    -- can never again be published as a trend.
    CASE
        WHEN ta_cur.households_in_ta IS NULL THEN 'no_current_data'
        WHEN ta_cur.households_in_ta = 0 THEN 'submission_gap'
        WHEN ta_prev.households_in_ta IS NULL OR ta_prev.households_in_ta = 0 THEN 'no_prior_year'
        WHEN (ta_cur.households_in_ta - ta_prev.households_in_ta)::NUMERIC
             / NULLIF(ta_prev.households_in_ta, 0) * 100 > 10  THEN 'rising_strongly'
        WHEN (ta_cur.households_in_ta - ta_prev.households_in_ta)::NUMERIC
             / NULLIF(ta_prev.households_in_ta, 0) * 100 > 3   THEN 'rising'
        WHEN (ta_cur.households_in_ta - ta_prev.households_in_ta)::NUMERIC
             / NULLIF(ta_prev.households_in_ta, 0) * 100 > -3  THEN 'flat'
        WHEN (ta_cur.households_in_ta - ta_prev.households_in_ta)::NUMERIC
             / NULLIF(ta_prev.households_in_ta, 0) * 100 > -10 THEN 'falling'
        WHEN (ta_cur.households_in_ta - ta_prev.households_in_ta)::NUMERIC
             / NULLIF(ta_prev.households_in_ta, 0) * 100 <= -10 THEN 'falling_strongly'
        ELSE 'undetermined'
    END AS ta_trend_label,"""

OLD_DQ = """        'ta_current', CASE WHEN ta_cur.households_in_ta = 0 THEN 'submission_gap' ELSE 'ok' END,
        'ta_trend',   CASE WHEN ta_prev.households_in_ta IS NULL THEN 'no_prior_year' ELSE 'ok' END,"""

NEW_DQ = """        'ta_current', CASE
            WHEN ta_cur.households_in_ta IS NULL THEN 'missing'
            WHEN ta_cur.households_in_ta = 0 THEN 'submission_gap'
            ELSE 'ok' END,
        'ta_trend',   CASE
            WHEN ta_cur.households_in_ta IS NULL THEN 'no_current_data'
            WHEN ta_prev.households_in_ta IS NULL THEN 'no_prior_year'
            ELSE 'ok' END,"""

REPLACEMENTS = [("ta_trend_label", OLD_TREND, NEW_TREND),
                ("data_quality", OLD_DQ, NEW_DQ)]


def connect(dbname):
    return psycopg2.connect(host="localhost",
                            port=int(ENV.get("PG_PORT", "5432")),
                            dbname=dbname, user=_require("PG_USER"),
                            password=_require("PG_PASSWORD"))


def patch(query):
    for name, old, new in REPLACEMENTS:
        if new in query:
            print(f"  {name}: already patched")
            continue
        if query.count(old) != 1:
            sys.exit(f"HALT: {name}: expected exactly one occurrence of the "
                     f"original text, found {query.count(old)}. The live node "
                     f"has drifted from what this patch was written against; "
                     f"re-read it before applying anything.")
        query = query.replace(old, new)
        print(f"  {name}: patched")
    return query


def assert_safe(query):
    problems = []
    if "ELSE 'falling_strongly'" in query:
        problems.append("a catch-all ELSE still yields 'falling_strongly'")
    if "'ta_current', CASE WHEN ta_cur.households_in_ta = 0 THEN " \
       "'submission_gap' ELSE 'ok' END" in query:
        problems.append("data_quality.ta_current still reports NULL as ok")
    if "WHEN ta_cur.households_in_ta IS NULL THEN 'no_current_data'" not in query:
        problems.append("the NULL-first branch is absent from ta_trend_label")
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    if not (args.check or args.apply):
        ap.error("choose --check or --apply")

    conn = connect("n8ndb")
    cur = conn.cursor()
    try:
        cur.execute("SELECT nodes FROM workflow_entity WHERE id = %s",
                    (WORKFLOW_ID,))
        row = cur.fetchone()
        if row is None:
            sys.exit(f"HALT: workflow {WORKFLOW_ID} not found in n8ndb")
        nodes = row[0]
        if isinstance(nodes, str):
            nodes = json.loads(nodes)

        target = None
        for n in nodes:
            if n.get("name") == NODE:
                target = n
                break
        if target is None:
            sys.exit(f"HALT: node {NODE!r} not found in the workflow")

        original = target["parameters"]["query"]
        print(f"live node {NODE!r}: {len(original)} chars")
        patched = patch(original)
        problems = assert_safe(patched)
        if problems:
            sys.exit("HALT: patched query still unsafe:\n  " +
                     "\n  ".join(problems))
        print("  assertions: NULL-first branch present, no catch-all direction")

        if not args.apply:
            conn.rollback()
            print("\n--check only, nothing written.")
            return 0

        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = BACKUP_DIR / f"w1_node5_backup_{stamp}.json"
        backup.write_text(json.dumps(nodes, indent=2), encoding="utf-8")
        print(f"\nprevious node JSON backed up to {backup.name}")

        target["parameters"]["query"] = patched
        cur.execute('UPDATE workflow_entity SET nodes = %s, "updatedAt" = now() '
                    "WHERE id = %s", (json.dumps(nodes), WORKFLOW_ID))
        if cur.rowcount != 1:
            conn.rollback()
            sys.exit(f"HALT: update touched {cur.rowcount} rows, expected 1")

        cur.execute("SELECT nodes FROM workflow_entity WHERE id = %s",
                    (WORKFLOW_ID,))
        back = cur.fetchone()[0]
        if isinstance(back, str):
            back = json.loads(back)
        live = next(n["parameters"]["query"] for n in back if n["name"] == NODE)
        if live != patched or assert_safe(live):
            conn.rollback()
            sys.exit("HALT: read-back does not match what was written")
        conn.commit()
        print("n8ndb updated and read back clean. COMMITTED")

        if DOC.exists():
            text = DOC.read_text(encoding="utf-8")
            for _, old, new in REPLACEMENTS:
                if old in text:
                    text = text.replace(old, new)
            DOC.write_text(text, encoding="utf-8")
            print(f"{DOC.relative_to(REPO)} updated to match the live node")
        return 0
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
