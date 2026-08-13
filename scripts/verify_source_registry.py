"""Hard gates for the source refresh control layer.

Every check is a gate. Any failure aborts the build; nothing downstream —
including the GitHub publish — should run on a red table.

Gate 9 proves idempotency by running the backfill a second time and diffing
the whole table, so it needs the backfill script to be present and runnable.

Usage:
    python scripts/verify_source_registry.py
    python scripts/verify_source_registry.py --skip-idempotency
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _db import get_conn  # noqa: E402

REPO = Path(__file__).resolve().parent.parent

# Windows consoles default to cp1252; this output contains em dashes and
# arrows lifted verbatim from the documentation.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BACKFILL = Path(__file__).resolve().parent / "backfill_source_registry.py"

VOCAB = {
    "acquisition_method": {"api", "landing_page", "manual", "derived"},
    "cadence": {"monthly", "quarterly", "annual", "periodic", "static"},
    "geography_level": {"LAD24", "UTLA", "BRMA", "PFA", "LSOA", "entity", "none"},
    "ucws_lens": {"primary", "secondary", "context", "infrastructure", "none"},
    "hss_lens": {"primary", "secondary", "context", "infrastructure", "none"},
    "refresh_tier": {"A", "B", "C"},
    "status": {"active", "pending_build", "deprecated", "superseded"},
}
EXPECTED_CONSTRAINTS = [
    "source_registry_acquisition_method_chk",
    "source_registry_cadence_chk",
    "source_registry_geography_level_chk",
    "source_registry_ucws_lens_chk",
    "source_registry_hss_lens_chk",
    "source_registry_refresh_tier_chk",
    "source_registry_status_chk",
    "source_registry_superseded_by_not_self_chk",
    "source_check_log_check_method_chk",
    "source_check_log_outcome_chk",
    # New pipeline_run_log writes are constrained to 'success'.
    # NOT VALID, so the two historical 'complete' rows stand.
    "pipeline_run_log_status_new_writes_chk",
]
NOT_NULL_COLS = ["source_code", "source_name", "publisher",
                 "acquisition_method", "cadence", "refresh_tier", "status"]
KNOWN_RUN_STATUSES = {"success", "complete"}

results = []


def gate(n, name, passed, detail):
    results.append((n, name, passed, detail))


def find_methodology():
    for cand in (REPO / "docs" / "METHODOLOGY.md",
                 REPO / "ONS_Population_Estimates" / "docs" / "METHODOLOGY.md"):
        if cand.exists():
            return cand
    sys.exit("HALT: docs/METHODOLOGY.md not found")


def register_codes():
    codes, in_table = [], False
    for line in find_methodology().read_text(encoding="utf-8").splitlines():
        if line.startswith("| S# | Source |"):
            in_table = True
            continue
        if in_table:
            if not line.startswith("|"):
                break
            first = line.strip().strip("|").split("|")[0].strip()
            if set(first) <= set("-: "):
                continue
            codes.append(first)
    return codes


def snapshot(cur):
    """Every registry row as a comparable tuple, for the idempotency diff."""
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema='public' AND table_name='source_registry'
          AND column_name NOT IN ('created_at','updated_at')
        ORDER BY ordinal_position
    """)
    cols = [r[0] for r in cur.fetchall()]
    cur.execute(f"SELECT {', '.join(cols)} FROM source_registry ORDER BY source_code")
    return cols, {r[0]: dict(zip(cols, (str(v) for v in r)))
                  for r in cur.fetchall()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-idempotency", action="store_true")
    args = ap.parse_args()

    conn = get_conn()
    cur = conn.cursor()

    reg = register_codes()

    # ---- Gate 1: row count matches discovery -----------------------------
    cur.execute("SELECT COUNT(*) FROM source_registry")
    n_rows = cur.fetchone()[0]
    gate(1, "row count equals the discovered source count",
         n_rows == len(reg),
         f"source_registry has {n_rows} rows; docs/METHODOLOGY.md (the "
         f"source register, and the source of truth for this number) "
         f"registers {len(reg)}")

    # ---- Gate 2: registry and register agree both ways -------------------
    cur.execute("SELECT source_code, status FROM source_registry")
    rows = dict(cur.fetchall())
    missing = sorted(set(reg) - set(rows))
    extra = sorted(c for c, st in rows.items()
                   if c not in reg and st != "pending_build")
    gate(2, "every register source has a row, and vice versa",
         not missing and not extra,
         f"in METHODOLOGY but not the registry: {missing or 'none'}; "
         f"in the registry, not in METHODOLOGY, not pending_build: "
         f"{extra or 'none'}")

    # ---- Gate 3: no empty string masquerading as a value -----------------
    conds = " OR ".join(f"btrim({c}) = ''" for c in NOT_NULL_COLS)
    cur.execute(f"SELECT source_code FROM source_registry WHERE {conds}")
    empties = [r[0] for r in cur.fetchall()]
    gate(3, "no NOT NULL column holds an empty string",
         not empties,
         f"offending sources: {empties or 'none'} "
         f"(checked {', '.join(NOT_NULL_COLS)})")

    # ---- Gate 4: vocabularies, and the constraints that enforce them -----
    bad = []
    for col, allowed in VOCAB.items():
        cur.execute(f"SELECT DISTINCT {col} FROM source_registry "
                    f"WHERE {col} IS NOT NULL")
        for (v,) in cur.fetchall():
            if v not in allowed:
                bad.append(f"{col}={v!r}")
    cur.execute("SELECT conname FROM pg_constraint WHERE conname = ANY(%s)",
                (EXPECTED_CONSTRAINTS,))
    present = {r[0] for r in cur.fetchall()}
    absent = [c for c in EXPECTED_CONSTRAINTS if c not in present]
    gate(4, "controlled vocabularies hold, and the CHECK constraints exist",
         not bad and not absent,
         f"out-of-vocabulary values: {bad or 'none'}; "
         f"missing CHECK constraints: {absent or 'none'} "
         f"({len(present)}/{len(EXPECTED_CONSTRAINTS)} present)")

    # ---- Gate 5: the view is one row per active registry row -------------
    cur.execute("SELECT COUNT(*) FROM vw_source_due")
    v_rows = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM (SELECT source_code FROM vw_source_due "
                "GROUP BY source_code HAVING COUNT(*) > 1) d")
    dupes = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM source_registry WHERE status = 'active'")
    active = cur.fetchone()[0]
    gate(5, "vw_source_due is one row per registry row, no join fan-out",
         v_rows == n_rows and dupes == 0,
         f"view rows {v_rows}, registry rows {n_rows}, active {active}, "
         f"duplicated source_codes {dupes}")

    # ---- Gate 6: no source borrows another source's run ------------------
    # Assert for every parent/sub-source pair the register carries, and for
    # every run-log row whose source_code is null.
    pairs = []
    for code in reg:
        m = re.match(r"^(\d+)[a-z]$", code)
        if m and m.group(1) in reg:
            pairs.append((m.group(1), code))
    problems = []

    # Attribution is asserted by run-log id, never by timestamp. Runs 57 and
    # 58 (S9a and S9b) carry the identical started_at, so a timestamp join
    # matches both and reports a conflict that does not exist.
    cur.execute("""
        SELECT id, source_code, source_number, agent_name, status,
               COALESCE(completed_at, started_at) AS success_at
        FROM pipeline_run_log
        ORDER BY id
    """)
    log = cur.fetchall()
    resolved_numbers = {sn for _, sc, sn, _, _, _ in log
                        if sc is not None and sn is not None}

    attributed = {}   # source_code -> [(run id, how, success_at)]
    for run_id, sc, sn, agent, status, success_at in log:
        if status not in KNOWN_RUN_STATUSES:
            continue
        if sc is not None:
            attributed.setdefault(sc, []).append((run_id, "source_code",
                                                  success_at, agent))
        elif sn is not None and sn not in resolved_numbers:
            attributed.setdefault(sn, []).append((run_id, "source_number",
                                                  success_at, agent))

    # A row attributed by number fallback must not name a different series.
    names = {}
    for line in find_methodology().read_text(encoding="utf-8").splitlines():
        if line.startswith("|") and "|" in line[1:]:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) > 1 and cells[0] in reg:
                names[cells[0]] = cells[1]
    for code, runs in attributed.items():
        for run_id, how, _, agent in runs:
            if how != "source_number":
                continue
            for other, nm in names.items():
                if other != code and nm and agent and nm.lower() in agent.lower():
                    problems.append(
                        f"run {run_id} ({agent}) is attributed to S{code} by "
                        f"source_number fallback but names S{other}'s series")

    # Every parent/sub-source pair must have disjoint attribution sets.
    for parent, child in pairs:
        p_ids = {r[0] for r in attributed.get(parent, [])}
        c_ids = {r[0] for r in attributed.get(child, [])}
        if p_ids & c_ids:
            problems.append(f"S{parent} and S{child} share run ids "
                            f"{sorted(p_ids & c_ids)}")

    # The view's last_success_at must equal the max over that source's own
    # attributed runs, and no other source's.
    cur.execute("SELECT source_code, last_success_at FROM vw_source_due")
    for code, last in cur.fetchall():
        own = [s for _, _, s, _ in attributed.get(code, []) if s is not None]
        expected = max(own) if own else None
        if last != expected:
            problems.append(f"S{code} last_success_at {last} does not equal "
                            f"the max over its own attributed runs "
                            f"({expected})")

    gate(6, "no source resolves to another source's run-log row",
         not problems,
         f"parent/sub-source pairs checked: "
         f"{[f'{p}/{c}' for p, c in pairs] or 'none'}; "
         f"{sum(len(v) for v in attributed.values())} successful runs "
         f"attributed across {len(attributed)} sources; "
         f"problems: {problems or 'none'}")

    # ---- Gate 6b: run-log status vocabulary -----------------------------
    cur.execute("SELECT DISTINCT status FROM pipeline_run_log")
    seen = {r[0] for r in cur.fetchall()}
    unknown = sorted(seen - KNOWN_RUN_STATUSES)
    gate("6b", "pipeline_run_log statuses are all known to vw_source_due",
         not unknown,
         f"statuses present: {sorted(seen)}; unknown to the view's success "
         f"whitelist: {unknown or 'none'}")

    # ---- Gate 7: confidential implies not published ----------------------
    cur.execute("""
        SELECT source_code FROM source_registry
        WHERE confidential = true
          AND (publish_github = true OR publish_map = true)
    """)
    leaks = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT COUNT(*) FROM source_registry WHERE confidential")
    n_conf = cur.fetchone()[0]
    gate(7, "every confidential source is unpublished on both channels",
         not leaks,
         f"{n_conf} confidential source(s); violations: {leaks or 'none'}")

    # ---- Gate 8: superseded_by is clean ----------------------------------
    cur.execute("""
        SELECT r.source_code, r.superseded_by
        FROM source_registry r
        LEFT JOIN source_registry t ON t.source_code = r.superseded_by
        WHERE r.superseded_by IS NOT NULL
          AND (t.source_code IS NULL OR r.superseded_by = r.source_code)
    """)
    dangling = cur.fetchall()
    gate(8, "superseded_by has no dangling or self references",
         not dangling, f"violations: {dangling or 'none'}")

    # ---- Gate 9: idempotency ---------------------------------------------
    if args.skip_idempotency:
        gate(9, "backfill is idempotent", None, "skipped by flag")
    else:
        cols, before = snapshot(cur)
        conn.commit()
        proc = subprocess.run([sys.executable, str(BACKFILL)],
                              capture_output=True, text=True, cwd=str(REPO))
        if proc.returncode != 0:
            gate(9, "backfill is idempotent", False,
                 f"re-run exited {proc.returncode}: "
                 f"{proc.stderr.strip()[:300]}")
        else:
            cur.execute("SELECT 1")  # refresh the snapshot on a live cursor
            _, after = snapshot(cur)
            changed = []
            if set(before) != set(after):
                changed.append(f"row set changed: "
                               f"{sorted(set(after) ^ set(before))}")
            for code in sorted(set(before) & set(after)):
                for c in cols:
                    b, a = before[code][c], after[code][c]
                    if b != a:
                        changed.append(f"S{code}.{c}: {b!r} -> {a!r}")
            gate(9, "backfill is idempotent",
                 not changed,
                 f"rows before {len(before)}, after {len(after)}; "
                 f"field changes: {changed or 'none'}")

    # ---- Gate 10: generated block excludes unpublished sources -----------
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parent /
                             "generate_methodology.py")],
        capture_output=True, text=True, cwd=str(REPO))
    out = proc.stdout
    cur.execute("SELECT source_code, source_name FROM source_registry "
                "WHERE publish_github = false")
    hidden = cur.fetchall()
    # Only inspect the generated block itself, not the surrounding commentary.
    block = out.split("| S# |", 1)[-1] if "| S# |" in out else ""
    found = [c for c, name in hidden
             if re.search(rf"^\|\s*{re.escape(c)}\s*\|", block, re.M)
             or (name and name in block)]
    gate(10, "the generated block contains no publish_github = false source",
         proc.returncode == 0 and not found,
         f"withheld sources: {[c for c, _ in hidden] or 'none'}; "
         f"any that leaked into the block: {found or 'none'}")

    cur.close()
    conn.close()

    # ---- Report -----------------------------------------------------------
    width = max(len(name) for _, name, _, _ in results)
    print()
    print(f"{'#':<4}{'GATE':<{width + 2}}RESULT")
    print("-" * (width + 14))
    failed = 0
    for n, name, passed, _ in results:
        mark = "SKIP" if passed is None else ("PASS" if passed else "FAIL")
        if passed is False:
            failed += 1
        print(f"{str(n):<4}{name:<{width + 2}}{mark}")
    print()
    for n, name, passed, detail in results:
        mark = "SKIP" if passed is None else ("PASS" if passed else "FAIL")
        print(f"[{mark}] {n}. {name}")
        print(f"       {detail}")
    print()
    if failed:
        print(f"{failed} gate(s) FAILED. The build is not complete and "
              f"nothing should be published.")
        return 1
    print("All gates passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
