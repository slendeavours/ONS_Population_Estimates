"""Run Workflow 1 from the step files in sql/w1.

The eight steps run in n8n's connection order: Create Staging Tables, Signal
Column Pre-flight, Create Run, National Aggregates, LA Signals, Tenant Type
Rankings, Section 3 Top 3 LAs, Mark Run Complete. All eight run in one
transaction, committed only after Mark Run Complete has succeeded. Any
failure rolls the whole run back, including the new staging_runs row.

Before the run, as w1_rerun.py did:
  - the guards: the LA Signals step carries the NULL-first trend branch and
    has no catch-all ELSE 'falling_strongly';
  - the column contract check (w1_contract_check), which also refreshes
    staging_signal_contract. Step 02 compares the table against that
    contract, so it has to be refreshed first. It is committed on its own,
    outside the run transaction, in every mode, including --dry-run and
    --simulate;
  - the staging_runs sequence is moved past any run_id already in
    staging_la_signals (committed on its own; skipped under --simulate).

The one-completed-run-per-day rule is enforced by the Create Run step itself.

Usage:
    python scripts/w1_run.py --dry-run    guards and contract check only
    python scripts/w1_run.py --simulate   run all eight steps, then roll back
    python scripts/w1_run.py              the real run (commits)
"""
import argparse
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _db import get_conn, get_readonly_conn  # noqa: E402
import w1_contract_check  # noqa: E402
from w1_steps import STEPS, read_step  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CREATE_RUN = "Create Run"
MARK_RUN_COMPLETE = "Mark Run Complete"
NULL_BRANCH = "WHEN ta_cur.households_in_ta IS NULL THEN 'no_current_data'"
CATCH_ALL = "ELSE 'falling_strongly'"


# ── Helpers copied from s22_w1_wire.py ──────────────────────────────────────
# Copied rather than imported: s22_w1_wire is a one-off n8n script that is
# being moved to scripts/historical. Behaviour is unchanged.

def log(m):
    """Short timestamped progress line (s22_w1_wire.log)."""
    print(f"[{datetime.datetime.now():%H:%M:%S}] {m}", flush=True)


def _pg(sql):
    """n8n Postgres node placeholders -> psycopg2 named parameters.

    Copied from s22_w1_wire._pg. The node passed queryReplacement into $1.
    psycopg2 does not understand $1, so it becomes a named parameter, which
    also handles the queries that use $1 ten times. Literal per-cent signs
    (comments, RAISE format strings) are escaped first so they are not read
    as placeholders. The query stays parameterised; nothing is concatenated
    in.
    """
    return sql.replace("%", "%%").replace("$1", "%(run_id)s")


def _run_id_high_water(cur):
    """(sequence last_value, highest run_id in staging_runs or signals)."""
    cur.execute("""
        SELECT GREATEST(COALESCE((SELECT MAX(run_id) FROM staging_runs), 0),
                        COALESCE((SELECT MAX(run_id) FROM staging_la_signals), 0))
    """)
    high = cur.fetchone()[0]
    cur.execute("SELECT last_value FROM staging_runs_run_id_seq")
    seq = cur.fetchone()[0]
    return seq, high


def align_run_sequence(conn, warnings):
    """staging_runs.run_id must lead every run_id already in the staging data.

    Copied from s22_w1_wire.align_run_sequence. Runs 10 and 11 were written
    to staging_la_signals by direct SQL without a matching staging_runs row,
    so the sequence trails the data and the next nextval() would collide
    with an existing signals run. Commits when it moves the sequence.
    """
    cur = conn.cursor()
    seq, high = _run_id_high_water(cur)
    if seq <= high:
        cur.execute("SELECT setval('staging_runs_run_id_seq', %s, true)",
                    (high,))
        conn.commit()
        msg = (f"staging_runs sequence was at {seq} but staging_la_signals "
               f"already holds run_id {high}; runs 10 and 11 were written by "
               "direct SQL with no staging_runs row. Sequence advanced to "
               f"{high} so the new run does not collide.")
        warnings.append(msg)
        log(f"  {msg}")


# ── Guards and the run ──────────────────────────────────────────────────────

def check_guards(sql_by_name):
    """Problems with the step SQL about to run; empty means pass."""
    sig = sql_by_name.get("LA Signals")
    if not sig:
        return ["the LA Signals step is missing or empty"]
    problems = []
    if NULL_BRANCH not in sig:
        problems.append("the LA Signals step does not carry the NULL-first "
                        f"trend branch ({NULL_BRANCH}).")
    if CATCH_ALL in sig:
        problems.append("the LA Signals step still has a catch-all "
                        f"{CATCH_ALL}.")
    return problems


def _validate(steps):
    names = [n for n, _ in steps]
    if names.count(CREATE_RUN) != 1:
        raise ValueError(f"exactly one '{CREATE_RUN}' step is needed, "
                         f"got {names.count(CREATE_RUN)}")
    if not names or names[-1] != MARK_RUN_COMPLETE \
            or names.count(MARK_RUN_COMPLETE) != 1:
        raise ValueError(f"'{MARK_RUN_COMPLETE}' must be the last step, "
                         f"once; order given: {names}")


def run_steps(conn, steps, *, simulate=False, on_complete=None):
    """Run (name, sql) pairs in order in one transaction; return the run id.

    Steps up to and including Create Run are executed as written, with no
    parameters (02 and 03 hold RAISE format strings that _pg would alter).
    Create Run returns the new run_id. Every later step goes through _pg and
    receives it as %(run_id)s. Commits once, after the last step (Mark Run
    Complete), unless simulate is True, in which case it always rolls back.
    Any exception rolls back everything and is re-raised.

    on_complete(cur, run_id), if given, runs after the last step and before
    the commit or rollback (used to report the trend labels of a simulated
    run before they disappear).
    """
    _validate(steps)
    notices = conn.notices if isinstance(
        getattr(conn, "notices", None), list) else None
    seen = len(notices) if notices is not None else 0
    cur = conn.cursor()
    run_id = None
    try:
        for name, sql in steps:
            if run_id is None:
                cur.execute(sql)
                if name == CREATE_RUN:
                    row = cur.fetchone()
                    if not row or row[0] is None:
                        raise RuntimeError("Create Run returned no run_id")
                    run_id = row[0]
                    log(f"W1 run_id {run_id}")
                    continue
            else:
                cur.execute(_pg(sql), {"run_id": run_id})
            log(f"  {name}: {cur.rowcount} row(s)")
            if notices is not None:
                for n in notices[seen:]:
                    log(f"     {n.strip()}")
                seen = len(notices)
        if on_complete is not None:
            on_complete(cur, run_id)
        if simulate:
            conn.rollback()
            log("--simulate: every step ran; the run transaction is rolled "
                "back, nothing from it is kept")
        else:
            conn.commit()
            log("  committed: run marked complete")
        return run_id
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def _report_labels(cur, run_id):
    cur.execute("""
        SELECT ta_trend_label, COUNT(*) FROM staging_la_signals
        WHERE run_id = %s GROUP BY 1 ORDER BY 2 DESC
    """, (run_id,))
    log("  trend labels this run:")
    for label, n in cur.fetchall():
        log(f"     {str(label):<18} {n}")


def _contract_snapshot():
    """staging_signal_contract content, ignoring recorded_at; None if absent."""
    conn = get_readonly_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('public.staging_signal_contract')")
        if cur.fetchone()[0] is None:
            return None
        cur.execute("""
            SELECT column_name, ordinal, source_expr, refreshed_on_conflict,
                   node_query_sha256
              FROM staging_signal_contract ORDER BY ordinal, column_name""")
        return cur.fetchall()
    finally:
        conn.rollback()
        conn.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="guards and contract check only, run no step")
    mode.add_argument("--simulate", action="store_true",
                      help="run all eight steps in one transaction, then "
                           "roll it back")
    args = ap.parse_args(argv)

    if args.dry_run or args.simulate:
        log("note: the contract check refreshes staging_signal_contract and "
            "commits that on its own, even under --dry-run and --simulate. "
            "It is the only write outside the run transaction.")

    sql_by_name = {s.name: read_step(s) for s in STEPS}
    empty = [n for n, sql in sql_by_name.items() if not sql.strip()]
    if empty:
        print(f"HALT: empty step file(s): {empty}", file=sys.stderr)
        return 1
    problems = check_guards(sql_by_name)
    if problems:
        for p in problems:
            print(f"HALT: {p}", file=sys.stderr)
        return 1
    log("guards pass: LA Signals carries the NULL-safe labels")

    before = _contract_snapshot()
    try:
        errors, warnings, _ = w1_contract_check.check()
    except ValueError as e:
        print(f"HALT: W1 contract check could not run: {e}", file=sys.stderr)
        return 1
    for w in warnings:
        log(f"  contract WARN {w}")
    if errors:
        for e in errors:
            log(f"  contract ERROR {e}")
        print("HALT: the LA Signals / staging_la_signals column contract "
              "does not hold", file=sys.stderr)
        return 1
    log("column contract holds in both directions")
    after = _contract_snapshot()
    log("  staging_signal_contract: "
        + ("content unchanged (recorded_at refreshed)" if before == after
           else "content changed by this refresh"))

    if args.dry_run:
        log("--dry-run, no step executed")
        return 0

    steps = [(s.name, sql_by_name[s.name]) for s in STEPS]
    conn = get_conn()
    conn.autocommit = False
    try:
        if args.simulate:
            seq, high = _run_id_high_water(conn.cursor())
            if seq <= high:
                log(f"  --simulate: staging_runs sequence is at {seq}, highest "
                    f"run_id in use is {high}; a real run would call "
                    f"setval(..., {high}) and commit first"
                    + (" (same value, so no real change)" if seq == high
                       else "") + ". Skipped here.")
            else:
                log(f"  staging_runs sequence at {seq}, ahead of run_id "
                    f"{high}: no adjustment needed")
            log("  --simulate: Create Run will still consume one sequence "
                "value; sequences are not rolled back")
        else:
            align_run_sequence(conn, [])
        run_id = run_steps(conn, steps, simulate=args.simulate,
                           on_complete=_report_labels)
    finally:
        conn.close()
    print(run_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
