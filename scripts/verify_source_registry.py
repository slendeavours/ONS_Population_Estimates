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
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _db import get_readonly_conn, readonly_identity  # noqa: E402

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

    # Read-only. Gate 9 shells out to the backfill, which opens its own
    # write connection; this suite itself cannot write.
    conn = get_readonly_conn()
    cur = conn.cursor()
    ro_user, dedicated = readonly_identity()

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
    # The outcome vocabulary was widened for revising sources. Assert the
    # widening actually reached the constraint, not just the code.
    cur.execute("""SELECT pg_get_constraintdef(oid) FROM pg_constraint
                   WHERE conname = 'source_check_log_outcome_chk'""")
    defn = (cur.fetchone() or [""])[0]
    for token in ('no_change', 'new_edition', 'url_changed', 'check_failed',
                  'revision_detected'):
        if token not in defn:
            absent.append(f"source_check_log outcome missing {token!r}")
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
        conn.rollback()
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

    # ---- Gate 11: every script resolves .env from the published checkout ----
    # This defect has appeared twice: register_lib.py, then six scripts that
    # looked for .env only at the repository root. That is correct from the
    # outer working copy and wrong inside the published one, where .env sits
    # a level up. Two incidents is a class, and the next script written would
    # reintroduce it. scripts/_db.py is the reference implementation, so this
    # gate has something to assert against.
    script_dir = Path(__file__).resolve().parent
    offenders, checked = [], 0
    for path in sorted(script_dir.glob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        # '.env' as a bare substring also matches os.environ, which is an
        # environment variable read and not a dotenv file at all. That false
        # positive pulled push.py and credential_scan.py into scope on
        # 2026-08-16 for containing dict(os.environ, ...). The test is now the
        # filename in the forms it is actually written, plus any script that
        # reaches the database - so a script that delegates to _db stays
        # asserted rather than dropping out of scope.
        DOTENV_REFS = ('".env"', "'.env'", "/.env", ".env file", "dotenv")
        needs_env = ("PG_PASSWORD" in text
                     or any(s in text for s in DOTENV_REFS)
                     or "from _db import" in text)
        if not needs_env or path.name == "_db.py":
            continue
        checked += 1
        # Either delegate to _db, or look in both locations.
        delegates = "from _db import" in text
        two_places = (('ROOT.parent / ".env"' in text)
                      or ('REPO.parent / ".env"' in text)
                      or ('_HERE.parent.parent / ".env"' in text))
        if not (delegates or two_places):
            offenders.append(path.name)
    gate(11, "every script resolves .env from the published checkout",
         not offenders,
         f"{checked} script(s) touch credentials or .env; "
         f"reference implementation is scripts/_db.py; "
         f"offenders: {offenders or 'none'}")

    # ---- Gate 12: no verification script commits -------------------------
    # A suite that can write is one wrong argument away from corrupting what
    # it verifies. That is not theoretical: s18_pipr_verify.py check 6 tested
    # idempotency by re-upserting and committing, and a run that fell back to
    # a stale default edition rewrote 71,442 rows of freshly loaded data.
    # Requiring the argument fixed the instance; this removes the capability.
    # Idempotency is now tested inside a transaction that is always rolled
    # back, with a content checksum compared either side.
    # The precise rule: a suite may record that it ran, and may not write the
    # data under test. pipeline_run_log is the one permitted target — s22's
    # node is verify-and-log by design, and forbidding that would be a rule
    # about tidiness rather than correctness.
    committers, suites = [], []
    for path in sorted(script_dir.glob("*verify*.py")):
        suites.append(path.name)
        text = path.read_text(encoding="utf-8", errors="replace")
        stripped = "\n".join(ln for ln in text.splitlines()
                             if not ln.lstrip().startswith("#"))
        targets = set()
        for m in re.finditer(
                r"(?<!DO )\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|TRUNCATE)\s+"
                r"([a-z_][a-z0-9_]*)", stripped, re.I):
            name = m.group(1).lower()
            if name != "set":          # 'DO UPDATE SET' is not a table
                targets.add(name)
        forbidden = sorted(targets - {"pipeline_run_log"})
        commits = bool(re.search(r"\bconn\.commit\(\)", stripped))
        # Writing the data under test is allowed only inside a probe that is
        # never committed. s18_pipr_verify re-upserts to test idempotency and
        # rolls back; that is fine. Committing such a write is what corrupted
        # 71,442 rows.
        if forbidden and commits:
            committers.append(f"{path.name} commits writes to {forbidden}")
    gate(12, "no verification script writes the data under test",
         not committers,
         f"suites checked: {suites or 'none'}; "
         f"connection: {ro_user}"
         f"{' (dedicated read-only role)' if dedicated else ''}"
         f"{'' if dedicated else ' — set PG_READONLY_USER/PG_READONLY_PASSWORD '
                                'to use ucws_readonly, which now holds SELECT '
                                'and no write grant'}; "
         f"committers: {committers or 'none'}")

    # ---- Gate 13: side registers agree with the data they describe --------
    # A provenance table that can disagree with the target table will
    # eventually disagree with it, in both directions, and nothing surfaces
    # that until someone reconciles by hand. homelessness_quarter_urls
    # disagreed twice and neither was visible for months: 2025Q1 marked
    # loaded with no rows in la_statutory_homelessness, and 2025Q3 loaded
    # with no register row.
    #
    # Per-row provenance cannot drift, because there is nothing to drift
    # from. Where a side register exists anyway, it has to agree.
    SIDE_REGISTERS = [
        # (register table, period column, loaded flag, target table, target period)
        ("homelessness_quarter_urls", "period", "loaded",
         "la_statutory_homelessness", "period"),
    ]
    drift = []
    for reg_t, reg_p, flag, tgt_t, tgt_p in SIDE_REGISTERS:
        cur.execute("""
            SELECT to_regclass(%s) IS NOT NULL, to_regclass(%s) IS NOT NULL
        """, (reg_t, tgt_t))
        reg_ok, tgt_ok = cur.fetchone()
        if not (reg_ok and tgt_ok):
            continue
        cur.execute(f"""
            SELECT r.{reg_p} FROM {reg_t} r
            WHERE r.{flag} IS TRUE
              AND NOT EXISTS (SELECT 1 FROM {tgt_t} t
                              WHERE t.{tgt_p} = r.{reg_p})
            ORDER BY 1
        """)
        claimed = [x[0] for x in cur.fetchall()]
        cur.execute(f"""
            SELECT DISTINCT t.{tgt_p} FROM {tgt_t} t
            WHERE NOT EXISTS (SELECT 1 FROM {reg_t} r
                              WHERE r.{reg_p} = t.{tgt_p})
            ORDER BY 1
        """)
        unrecorded = [x[0] for x in cur.fetchall()]
        if claimed:
            drift.append(f"{reg_t}: marked loaded but absent from {tgt_t}: "
                         f"{claimed}")
        if unrecorded:
            drift.append(f"{tgt_t}: loaded but unrecorded in {reg_t}: "
                         f"{unrecorded}")
    gate(13, "side registers agree with the data they describe",
         not drift,
         f"{len(SIDE_REGISTERS)} side register(s) checked; "
         f"disagreements: {drift or 'none'}")

    # ---- Gate 14: every stored lad24cd resolves to la_boundaries ----------
    # A source that stores the publisher's LA code without resolving it
    # through la_code_lookup produces rows that Workflow 1 cannot join to.
    # Not a halved figure - no matching row at all, and W1 then published the
    # resulting NULL as a trend. On 2026-08-14 this was true of
    # la_statutory_homelessness and la_rough_sleeping, and would have been
    # true of nhs_mh_crfd but for a view that happened to normalise on the way
    # out. Standing rule 3 has required resolution at extraction since
    # 2026-08-13; nothing enforced it.
    #
    # It was found by hand, by scanning forty tables. This turns that scan
    # into something the build does every time. Base tables only: a view may
    # legitimately re-map codes, which is exactly what vw_mh_crfd_lad does.
    cur.execute("""
        SELECT c.table_name
        FROM information_schema.columns c
        JOIN pg_class p ON p.relname = c.table_name
        JOIN pg_namespace n ON n.oid = p.relnamespace AND n.nspname = 'public'
        WHERE c.column_name = 'lad24cd'
          AND c.table_schema = 'public'
          AND p.relkind = 'r'
          AND c.table_name <> 'la_boundaries'
          -- _bak_YYYYMMDD tables are frozen copies taken before a rebuild.
          -- They are audit evidence of what a table was, so their codes must
          -- not be rewritten, and no product reads them. Gating them would
          -- produce a red that only deleting the evidence could clear.
          AND c.table_name !~ '_bak_[0-9]{8}$'
        ORDER BY 1
    """)
    #
    # A code may be knowably historical rather than unresolved. S.114 notices
    # are the case: Northamptonshire County Council issued two in 2018 and was
    # abolished in 2021, and the notice belongs to the entity that issued it.
    # It must not be propagated - one predecessor with two successors doubles
    # on any join by predecessor, which is the la_succession fan-out defect.
    #
    # A code may also be a live authority that is simply not a district. Care
    # leaver duties sit with upper tier, so care_leaver_accommodation stores 24
    # county councils on E10 codes that la_boundaries, a 296-code district set,
    # will never hold. Those are correct data, not orphans. They are accepted
    # on evidence - presence in utla_lad_mapping, the upper-tier reference -
    # rather than by declaration, because the evidence is already maintained
    # and a hand-written exemption would not be. Three of the 24 are abolished
    # counties absent from that reference, and they take the predecessor route
    # above.
    #
    # So the exemption is a positive declaration, not a tolerated code. A row
    # is exempt only if its table carries an attribution column and the row
    # says attribution = 'predecessor' with successor codes and a note. An
    # orphan that declares nothing still fails. This narrows the gate: before,
    # any code in the table was judged only against la_boundaries; now an
    # unresolved code must either resolve or explain itself.
    lad_tables = [r[0] for r in cur.fetchall()]
    orphans, declared, upper_tier = [], [], []
    for t in lad_tables:
        cur.execute("""SELECT COUNT(*) FROM information_schema.columns
                       WHERE table_schema='public' AND table_name=%s
                         AND column_name IN ('attribution','successor_codes',
                                             'attribution_note')""", (t,))
        declarable = cur.fetchone()[0] == 3
        exempt = ("""AND NOT (t.attribution = 'predecessor'
                              AND t.successor_codes IS NOT NULL
                              AND t.attribution_note IS NOT NULL)"""
                  if declarable else "")
        cur.execute(f"""
            SELECT DISTINCT t.lad24cd FROM {t} t
            WHERE t.lad24cd IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM la_boundaries b
                              WHERE b.lad24cd = t.lad24cd)
              AND NOT EXISTS (SELECT 1 FROM utla_lad_mapping u
                              WHERE u.utla_code = t.lad24cd)
              {exempt}
            ORDER BY 1
        """)
        bad = [r[0] for r in cur.fetchall()]
        if bad:
            orphans.append(f"{t}: {bad}")
        if declarable:
            cur.execute(f"""SELECT DISTINCT t.lad24cd FROM {t} t
                            WHERE t.attribution = 'predecessor'
                              AND NOT EXISTS (SELECT 1 FROM la_boundaries b
                                              WHERE b.lad24cd = t.lad24cd)""")
            for (c,) in cur.fetchall():
                declared.append(f"{t}: {c}")
        cur.execute(f"""SELECT COUNT(DISTINCT t.lad24cd) FROM {t} t
                        WHERE EXISTS (SELECT 1 FROM utla_lad_mapping u
                                      WHERE u.utla_code = t.lad24cd)
                          AND NOT EXISTS (SELECT 1 FROM la_boundaries b
                                          WHERE b.lad24cd = t.lad24cd)""")
        n_ut = cur.fetchone()[0]
        if n_ut:
            upper_tier.append(f"{t}: {n_ut}")
    gate(14, "every stored lad24cd resolves to la_boundaries, or declares why not",
         not orphans,
         f"{len(lad_tables)} base table(s) carrying lad24cd checked against "
         f"the 296-code boundary set; "
         f"declared non-propagating predecessors (accepted): "
         f"{declared or 'none'}; "
         f"live upper-tier codes accepted via utla_lad_mapping: "
         f"{upper_tier or 'none'}; "
         f"tables holding an undeclared code la_boundaries does not have: "
         f"{orphans or 'none'}")

    # ---- Gate 15: no label stands on an absent measure ---------------------
    # Runs 4 to 12 published Sheffield and Barnsley with no TA figure and a
    # trend label of falling_strongly. The mechanism is three-valued logic, not
    # a wrong ELSE literal: a comparison against NULL yields NULL, which is
    # not-matched, so every WHEN arm declines and the ELSE catches it as a
    # positive assertion. The absent case then takes whatever the catch-all
    # says - here, the strongest downward signal on the scale.
    #
    # Asserted against the latest run only. Earlier runs are immutable audit
    # data and several are not reproducible, so gating them would produce a
    # permanent red that no fix could clear.
    #
    # A numeric derived value must be NULL when an input is absent - a number
    # over an absent measure is a fabrication. A categorical label is
    # different: ta_trend_label's whole purpose is to name the absence, so
    # requiring it to be NULL would forbid the correct behaviour. For those
    # columns the assertion is stricter, not looser - when an input is absent
    # the label must be one of the declared absence sentinels, and any
    # direction word is a violation. 'undetermined' is deliberately not a
    # sentinel: over an absent input it would mean the CASE fell through,
    # which is the exact defect this gate exists for.
    TA_ABSENT_OK = ("no_current_data", "no_prior_year", "submission_gap")
    LABEL_INPUTS = [
        ("staging_la_signals", "ta_trend_label",
         ["ta_households_current", "ta_households_prev_year"], TA_ABSENT_OK),
        ("staging_la_signals", "ta_yoy_pct",
         ["ta_households_current", "ta_households_prev_year"]),
        ("staging_la_signals", "marac_rate_per_10k",
         ["marac_cases", "population"]),
        ("staging_la_signals", "pip_rate_per_1000",
         ["pip_total_claimants", "population"]),
        ("staging_la_signals", "ctb_lte_rate_pct",
         ["ctb_empty_6m_plus", "ctb_total_dwellings"]),
        ("staging_national", "ta_yoy_pct",
         ["ta_households_current", "ta_households_prev_year"]),
    ]
    cur.execute("SELECT MAX(run_id) FROM staging_la_signals")
    latest_run = cur.fetchone()[0]
    standing = []
    for entry in LABEL_INPUTS:
        table, col, inputs = entry[0], entry[1], entry[2]
        absent_ok = entry[3] if len(entry) > 3 else None
        nulls = " OR ".join(f"{i} IS NULL" for i in inputs)
        if absent_ok is None:
            cond, params = f"{col} IS NOT NULL", (latest_run,)
        else:
            cond, params = f"{col} IS NULL OR {col} <> ALL(%s)", (latest_run, list(absent_ok))
        cur.execute(f"""SELECT COUNT(*) FROM {table}
                        WHERE run_id = %s AND ({nulls}) AND ({cond})""", params)
        n = cur.fetchone()[0]
        if n:
            standing.append(
                f"{table}.{col}: {n} row(s) over an absent "
                f"{' or '.join(inputs)}"
                + (f" not carrying one of {absent_ok}" if absent_ok else ""))
    gate(15, "no derived label stands on an absent measure",
         not standing,
         f"run {latest_run}, {len(LABEL_INPUTS)} derived column(s) checked "
         f"against their input measures; violations: {standing or 'none'}")


    # ---- Gate 16: national reconciles to the sum of its own LA rows --------
    # staging_national reads the source tables directly; staging_la_signals
    # reaches them through a join. For two authorities the join failed, so the
    # two were computed from different populations and nothing compared them:
    # runs 4 to 12 published a national TA total of 119,219 against a sum of
    # their own LA rows of 118,527 - a 692 gap, exactly Sheffield plus
    # Barnsley. No NULL test catches this; it needs the arithmetic checked
    # against itself.
    #
    # Every measure present in both tables is asserted. The one that is not a
    # sum is asserted on its correct relationship rather than skipped.
    SUM_MEASURES = [
        ("ta_households_current", "ta_households_current"),
        ("ta_households_prev_year", "ta_households_prev_year"),
        ("rough_sleeping_current", "rough_sleeping_current"),
        ("rough_sleeping_prev_year", "rough_sleeping_prev_year"),
        ("bb_spend_total_000", "ro4_bb_spend_000"),
        ("nightly_paid_spend_total_000", "ro4_nightly_spend_000"),
        ("hb_sa_caseload_total", "hb_sa_caseload"),
        ("housing_register_total", "housing_register"),
    ]
    cur.execute("SELECT MAX(run_id) FROM staging_national")
    nat_run = cur.fetchone()[0]
    breaks = []
    for nat_col, la_col in SUM_MEASURES:
        cur.execute(f"""
            SELECT n.{nat_col}, (SELECT SUM(s.{la_col})
                                 FROM staging_la_signals s
                                 WHERE s.run_id = n.run_id)
            FROM staging_national n WHERE n.run_id = %s
        """, (nat_run,))
        row = cur.fetchone()
        if not row:
            continue
        nat_v, la_v = row
        if nat_v is None and la_v is None:
            continue
        if nat_v is None or la_v is None or round(float(nat_v), 2) != round(float(la_v), 2):
            breaks.append(f"{nat_col}: national {nat_v} vs sum of LA rows {la_v}")

    # ta_yoy_pct is a ratio, not a sum. Summing per-authority percentages is
    # meaningless, so the relationship asserted is that the national
    # percentage equals the change between the two national totals.
    cur.execute("""
        SELECT ta_yoy_pct,
               ROUND((ta_households_current - ta_households_prev_year)::numeric
                     / NULLIF(ta_households_prev_year, 0) * 100, 2)
        FROM staging_national WHERE run_id = %s
    """, (nat_run,))
    row = cur.fetchone()
    if row and row[0] is not None and row[1] is not None:
        if round(float(row[0]), 2) != round(float(row[1]), 2):
            breaks.append(f"ta_yoy_pct: stored {row[0]} vs derived from the "
                          f"national totals {row[1]}")

    gate(16, "national totals reconcile to the sum of their own LA rows",
         not breaks,
         f"run {nat_run}, {len(SUM_MEASURES)} sum measure(s) plus ta_yoy_pct "
         f"asserted on its ratio relationship; disagreements: "
         f"{breaks or 'none'}")

    # ---- Gate 17: source_registry cannot be hand-edited ------------------
    # It is a generated table. 12 columns are regenerated for every source and
    # 27 more for the sources that declare them, so a direct edit either
    # reverts on the next backfill - which is how gate 9 caught one on
    # 2026-08-16 - or, worse, persists silently until somebody adds a
    # declaration for that source. The second case is the dangerous one: it
    # works until it doesn't, and nothing says when it stopped.
    #
    # The trigger makes the destination refuse the write outright. This gate
    # exists so the protection cannot be dropped without the suite noticing:
    # a control nobody checks is a control that has already gone.
    cur.execute("""
        SELECT tgenabled FROM pg_trigger
        WHERE tgrelid = 'source_registry'::regclass
          AND tgname = 'source_registry_writer_only_trg'
          AND NOT tgisinternal
    """)
    trg = cur.fetchone()
    gate(17, "source_registry is protected against direct edits",
         bool(trg) and trg[0] != 'D',
         "trigger source_registry_writer_only_trg: "
         + ("absent - direct edits are possible" if not trg
            else "disabled - direct edits are possible" if trg[0] == 'D'
            else "present and enabled; writers declare themselves with "
                 "SET ucws.registry_writer = 'on'"))

    cur.close()
    conn.close()


    # ---- Report -----------------------------------------------------------
    # Known-red gates are reported separately from new ones. Two permanently
    # failing gates would otherwise become the normal state, and the next
    # genuine failure would hide behind them: people stop reading the output
    # and start reading the exit code.
    #
    # Each known-red entry carries an owner and a date, and expires. Past its
    # date it counts as a new failure, which is what stops known_red.json
    # becoming a place defects go to be forgotten.
    known = {}
    kr_path = REPO / "docs" / "known_red.json"
    if kr_path.exists():
        for e in json.loads(kr_path.read_text(encoding="utf-8"))["known_red"]:
            known[str(e["gate"])] = e
    today = datetime.date.today()

    width = max(len(name) for _, name, _, _ in results)
    print()
    print(f"{'#':<4}{'GATE':<{width + 2}}RESULT")
    print("-" * (width + 22))
    # A known-red entry whose gate is currently passing is a defect in the
    # control itself. Proved 2026-08-16: gate 14 went green while its entry
    # was still in place, and an injected failure - a genuine one - was
    # absorbed as known-red so the suite exited 2 instead of 1. A stale entry
    # does not merely go unread, it swallows the next real failure of that
    # gate. Either the entry is stale or the gate is being masked, and neither
    # may be silent, so this is a hard failure like any other.
    gate_numbers = {str(n) for n, _, _, _ in results}
    stale_known, unknown_known = [], []

    new_failures, known_failures, expired = [], [], []
    for n, name, passed, _ in results:
        if passed is None:
            mark = "SKIP"
        elif passed:
            mark = "PASS"
            if str(n) in known:
                stale_known.append((n, name, known[str(n)]))
        else:
            entry = known.get(str(n))
            if not entry:
                mark = "FAIL (new)"
                new_failures.append((n, name))
            elif datetime.date.fromisoformat(entry["fix_by"]) < today:
                mark = "FAIL (overdue)"
                expired.append((n, name, entry))
            else:
                mark = "known-red"
                known_failures.append((n, name, entry))
        print(f"{str(n):<4}{name:<{width + 2}}{mark}")
    print()
    for n, name, passed, detail in results:
        mark = "SKIP" if passed is None else ("PASS" if passed else "FAIL")
        print(f"[{mark}] {n}. {name}")
        print(f"       {detail}")

    if known_failures:
        print()
        print("KNOWN-RED (owned, dated, not weakened):")
        for n, name, e in known_failures:
            days = (datetime.date.fromisoformat(e["fix_by"]) - today).days
            print(f"  gate {n}: {e['summary']}")
            print(f"     owner {e['owner']}, fix by {e['fix_by']} "
                  f"({days} days), {e['item']}")

    for g in sorted(known):
        if g not in gate_numbers:
            unknown_known.append(g)

    if stale_known or unknown_known:
        print()
        print("STALE KNOWN-RED:")
        for n, name, e in stale_known:
            print(f"  gate {n} PASSES but is still declared known-red "
                  f"({e['item']}).")
            print(f"     While that entry stands, a genuine failure of gate "
                  f"{n} reports as known-red and the suite exits 2 instead "
                  f"of 1. Remove the entry.")
        for g in unknown_known:
            print(f"  known_red.json declares gate {g}, which this suite does "
                  f"not define. The entry can never be cleared by a passing "
                  f"gate and masks nothing that exists. Remove it.")

    print()
    if expired:
        for n, name, e in expired:
            print(f"OVERDUE: gate {n} was due to be fixed by {e['fix_by']} "
                  f"({e['item']}). A known-red entry past its date is a "
                  f"failure, not a footnote.")
    if new_failures:
        for n, name in new_failures:
            print(f"NEW FAILURE: gate {n} - {name}")
    if new_failures or expired or stale_known or unknown_known:
        print()
        parts = [f"{len(new_failures)} new", f"{len(expired)} overdue"]
        if stale_known:
            parts.append(f"{len(stale_known)} stale known-red")
        if unknown_known:
            parts.append(f"{len(unknown_known)} unknown known-red")
        print(f"{', '.join(parts)} gate failure(s). Nothing should be "
              f"published.")
        return 1
    if known_failures:
        print(f"{len(known_failures)} known-red gate(s), all within date; no "
              f"new failures. Publishing is allowed, and the known-red list "
              f"is not.")
        return 2
    print("All gates passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
