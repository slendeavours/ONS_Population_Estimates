"""Period-editions engine: the generic loader logic on editions_core, driven
by a Profile.

Extracted from the reviewed S19 loader (scripts/s19_pip_editions.py) without
a change in behaviour: comparing a fetched period with its latest stored
edition, storing it (edition and live rows in one transaction, checked equal
inside it), the per-period loop with per-period savepoints, status with the
stranded-period check, the run log, and the ddl, status, sync-new and
refresh-latest command scaffolding. Source-specific work (discovery, fetch,
geography, building and checking records, migrations) stays in each loader.

Records are dicts holding the spec's key columns, its period column and
profile.value_cols. Periods are strings ('yyyymm' keys for S19, ISO dates for
S15 and S18); the engine never parses a period. Every period read from the
database is normalised to a string with str() (a DATE column gives
'2026-04-01'), so the callers only ever see strings.

Hooks: where a loader must be able to replace a step (its tests patch the
loader's own names), the functions take the step as a keyword callable, each
called (cur, profile, period, records, ...); the loader passes a callable
that looks its own name up at call time.

Blanks and zeros (docs/RULES.md rule 1): NULL equals NULL and differs from 0
in every comparison; a blank is never coerced to zero.
"""
import dataclasses
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import editions_core as core  # noqa: E402
import load_checks  # noqa: E402
from editions_core import halt  # noqa: E402

LIVE_MISSING = "live-missing"


@dataclass(frozen=True, kw_only=True)
class Profile:
    """One source's settings for the engine.

    spec: its EditionSpec. value_cols: the value columns a record carries and
    the comparison uses (each a value or extra column of the spec).
    run_agent / run_source: the pipeline_run_log agent_name and the
    source_number and source_code. heading: the status heading.
    default_source_file: an edition's source_file when the caller gives
    none. expected_areas: areas (distinct first-key values) a period's live
    rows must hold; None skips that check (live is still checked equal to
    the edition cell for cell). release_label(fetched_on) -> an edition's
    release_label. Optional: content_sha256(records) -> the edition's
    source_sha256 (default editions_core.rows_sha256); check_records(records,
    period) raises unless a fetched period is whole (run before anything is
    compared or stored); savepoint: the per-period savepoint name;
    example_label: the label of the values in compare examples (default the
    value columns, '(v1, v2)')."""
    spec: core.EditionSpec
    value_cols: tuple[str, ...]
    run_agent: str
    run_source: str
    heading: str
    default_source_file: str
    expected_areas: "int | None"
    release_label: Callable[[date], str]
    content_sha256: "Callable[[list], str] | None" = None
    check_records: "Callable[[list, str], None] | None" = None
    savepoint: str = "period_editions_period"
    example_label: "str | None" = None

    def __post_init__(self):
        data = ({c for c, _ in self.spec.value_cols}
                | {c for c, _ in self.spec.extra_cols})
        stray = [c for c in self.value_cols if c not in data]
        if not self.value_cols or stray:
            raise ValueError(f"{self.spec.name}: value_cols {stray or '()'} "
                             "are not value or extra columns of the spec")
        core._ident(self.savepoint)

    @property
    def label(self) -> str:
        return self.example_label or f"({', '.join(self.value_cols)})"

    def with_spec(self, spec: core.EditionSpec) -> "Profile":
        """This profile with another spec (a throwaway test copy)."""
        return self if spec is self.spec else dataclasses.replace(self, spec=spec)


def _p(v) -> str:
    """A period read from the database, as a string (rule: str(date))."""
    return v if isinstance(v, str) else str(v)


def _key(profile: Profile, r):
    """A record's (or row's) key: the single key value, or a tuple."""
    kc = profile.spec.key_cols
    return r[kc[0]] if len(kc) == 1 else tuple(r[k] for k in kc)


def _cols(profile: Profile) -> str:
    return ", ".join(tuple(profile.spec.key_cols) + tuple(profile.value_cols))


def _rows_by_key(profile: Profile, rows) -> dict:
    nk = len(profile.spec.key_cols)
    return {(row[0] if nk == 1 else tuple(row[:nk])): tuple(row[nk:])
            for row in rows}


# ---------------------------------------------------------------------------
# Compare and store
# ---------------------------------------------------------------------------

def stored(cur, profile: Profile, period: str, against: str):
    """({key: (values...)}, label) of what the period is compared with: its
    latest stored edition, or (against='live') the live table. Empty dict if
    the period has none. Halts on a broken chain."""
    spec, pc = profile.spec, profile.spec.period_col
    if against == "live":
        cur.execute(f"SELECT {_cols(profile)} FROM public.{spec.live_table} "
                    f"WHERE {pc} = %s", (period,))
        return _rows_by_key(profile, cur.fetchall()), "live"
    if against != "editions":
        raise ValueError(f"against must be 'editions' or 'live', not {against!r}")
    try:
        tip = core.chain_tip(cur, spec, period)
    except LookupError:
        return {}, "no edition"
    except ValueError as e:
        halt(f"{spec.editions_table}: {e}")
    cur.execute(f"SELECT {_cols(profile)} FROM public.{spec.editions_table} "
                f"WHERE {pc} = %s AND edition = %s", (period, tip))
    return _rows_by_key(profile, cur.fetchall()), f"edition {tip}"


def live_row_count(cur, profile: Profile, period: str) -> int:
    spec = profile.spec
    cur.execute(f"SELECT COUNT(*) FROM public.{spec.live_table} "
                f"WHERE {spec.period_col} = %s", (period,))
    return cur.fetchone()[0]


def live_missing_periods(cur, profile: Profile) -> list:
    """Periods with editions but no live rows (stranded). Ascending."""
    spec, pc = profile.spec, profile.spec.period_col
    cur.execute(f"SELECT DISTINCT e.{pc} FROM public.{spec.editions_table} e "
                f"WHERE NOT EXISTS (SELECT 1 FROM public.{spec.live_table} l "
                f"WHERE l.{pc} = e.{pc}) ORDER BY 1")
    return [_p(r[0]) for r in cur.fetchall()]


def insert_live(cur, profile: Profile, period: str, records: list) -> None:
    """The period's live rows (loaded_at takes its default, now())."""
    from psycopg2.extras import execute_values
    spec = profile.spec
    cols = tuple(spec.key_cols) + (spec.period_col,) + tuple(profile.value_cols)
    execute_values(cur, f"INSERT INTO public.{spec.live_table} "
                   f"({', '.join(cols)}) VALUES %s",
                   [tuple(r[k] for k in spec.key_cols) + (period,)
                    + tuple(r[c] for c in profile.value_cols) for r in records],
                   page_size=1000)


def check_live_equals_edition(cur, profile: Profile, period: str,
                              edition: int) -> list:
    """Problems unless the period's live rows equal the edition cell for
    cell (EXCEPT ALL both ways, NULL equals NULL, NULL differs from 0) and
    hold profile.expected_areas areas (distinct first-key values), one row
    each for a single key (with more key columns the rows are the edition's,
    which the cell-for-cell check already holds them to)."""
    spec = profile.spec
    bad = []
    n = core.rows_differing(cur, spec, period, edition)
    if n:
        bad.append(f"{period}: {n} live rows differ from edition {edition}")
    want_areas = profile.expected_areas
    if want_areas is None:
        return bad
    cur.execute(f"SELECT COUNT(DISTINCT {spec.key_cols[0]}), COUNT(*) "
                f"FROM public.{spec.live_table} WHERE {spec.period_col} = %s",
                (period,))
    got = tuple(cur.fetchone())
    want = (want_areas, want_areas if len(spec.key_cols) == 1 else got[1])
    if got != want:
        bad.append(f"{period}: live (areas, rows) {got}, expected "
                   f"({want[0]}, {want[1]})")
    return bad


def compare_period(cur, profile: Profile, period: str, records: list,
                   against: str = "editions") -> dict:
    """{kind, changed, examples, against}: kind 'new' (nothing stored),
    'unchanged' or 'revised'; changed counts areas whose values differ (an
    area on one side only counts; NULL equals NULL, NULL differs from 0); up
    to three examples. Against the editions, an unchanged period with no
    live rows is LIVE_MISSING: load inserts its live rows (no new edition)."""
    old, label = stored(cur, profile, period, against)
    new = {_key(profile, r): tuple(r[c] for c in profile.value_cols)
           for r in records}
    if not old:
        return {"kind": "new", "changed": len(new), "examples": [],
                "against": label}
    diff = [k for k in sorted(set(old) | set(new))
            if k not in old or k not in new or old[k] != new[k]]
    ex = [f"{k} {profile.label}: {old.get(k, 'absent')} -> "
          f"{new.get(k, 'absent')}" for k in diff[:3]]
    kind = "revised" if diff else "unchanged"
    if (kind == "unchanged" and against == "editions"
            and live_row_count(cur, profile, period) == 0):
        kind = LIVE_MISSING
    return {"kind": kind, "changed": len(diff), "examples": ex,
            "against": label}


def classify_period(cur, profile: Profile, period: str, records: list) -> str:
    """'new', 'unchanged', 'revised' or LIVE_MISSING against the period's
    latest stored edition."""
    return compare_period(cur, profile, period, records)["kind"]


def _live_into(cur, profile, period, records, edition, insert) -> None:
    """Insert the period's live rows unless live already holds the period,
    then halt unless live equals `edition` (and holds the expected areas)."""
    if live_row_count(cur, profile, period) == 0:
        insert(cur, profile, period, records)
    bad = check_live_equals_edition(cur, profile, period, edition)
    if bad:
        halt(f"{period}: the live rows failed their checks against edition "
             f"{edition}, month rolled back: " + "; ".join(bad))


def apply_period(cur, profile: Profile, period: str, records: list, *,
                 fetched_on: date, source_file: "str | None" = None,
                 classify=None, insert=None) -> str:
    """Classify, then store: edition 1 (supersedes None) AND the period's
    live rows when new; the next edition superseding the tip when revised
    (live is left alone: a revision reaches live only through
    refresh-latest); the live rows only (no edition) when LIVE_MISSING;
    nothing when unchanged. Checks the stored edition (tip, row count,
    distinct keys) and, for new and LIVE_MISSING, that live equals the
    edition cell for cell with the expected areas, before returning; any
    failure halts so the caller's per-period savepoint rolls the whole
    period back. Never commits. A return to the content of an older, non-tip
    edition is a revision against the tip (RULES 2.1) and is stored as a new
    edition: insert_edition(allow_revert=True). source_file is what the
    edition records (default profile.default_source_file).

    classify(cur, profile, period, records) -> kind and insert(cur, profile,
    period, records) default to classify_period and insert_live."""
    spec = profile.spec
    classify = classify or classify_period
    insert = insert or insert_live
    if source_file is None:
        source_file = profile.default_source_file
    kind = classify(cur, profile, period, records)
    if kind == "unchanged":
        return kind
    if kind == LIVE_MISSING:
        _live_into(cur, profile, period, records,
                   core.chain_tip(cur, spec, period), insert)
        return kind
    sha = (profile.content_sha256(records) if profile.content_sha256
           else core.rows_sha256(spec, records))
    tip = None if kind == "new" else core.chain_tip(cur, spec, period)
    ed = core.insert_edition(
        cur, spec, records, period,
        release_label=profile.release_label(fetched_on),
        published_date=fetched_on, source_file=source_file,
        source_sha256=sha, supersedes=tip, strict=True, allow_revert=True)
    bad = load_checks.check_coverage(cur, spec, period, ed, len(records))
    if core.chain_tip(cur, spec, period) != ed:
        bad.append(f"{period}: edition {ed} is not the chain tip")
    if bad:
        halt(f"{period} edition {ed} failed its checks: " + "; ".join(bad))
    if kind == "new":
        _live_into(cur, profile, period, records, ed, insert)
    return kind


def load_periods(cur, profile: Profile, periods, fetch, fetched_on: date,
                 commit: bool, *, simulate: bool = False,
                 against: "str | None" = None, stats: "dict | None" = None,
                 source_file: "str | None" = None, compare=None,
                 apply=None) -> int:
    """Fetch, check and compare each period; store it when commit or
    simulate. Each period runs in its own savepoint: on commit the period is
    committed on its own; on simulate, and in preview (neither flag: compare
    only, nothing applied), the savepoint is always rolled back. A failure
    (fetch, profile.check_records, check, halt) rolls that period back,
    stops, and returns 1; earlier committed periods stay. Returns 0 when
    every period went through. fetch(period) -> records is injected.
    against: 'editions' (default) or 'live'. If stats is given it is filled
    for the run log: periods, kinds, stored_rows (edition rows stored) and
    live_rows (live rows inserted).

    compare(cur, profile, period, records, against) -> dict and
    apply(cur, profile, period, records, *, fetched_on, source_file) -> kind
    default to compare_period and apply_period."""
    if commit and simulate:
        raise ValueError("commit and simulate are mutually exclusive")
    against = against or "editions"
    compare = compare or compare_period
    apply = apply or apply_period
    if source_file is None:
        source_file = profile.default_source_file
    sp = profile.savepoint
    write = commit or simulate
    conn = cur.connection
    tally = {"new": [0, 0], "unchanged": [0, 0], "revised": [0, 0],
             LIVE_MISSING: [0, 0]}
    if stats is not None:
        stats.update(periods=[], kinds={}, stored_rows=0, live_rows=0)
    for i, period in enumerate(periods):
        in_sp = False
        try:
            records = fetch(period)
            if profile.check_records is not None:
                profile.check_records(records, period)
            cur.execute(f"SAVEPOINT {sp}")
            in_sp = True
            cmp = compare(cur, profile, period, records, against)
            kind = cmp["kind"]
            if write:
                kind = apply(cur, profile, period, records,
                             fetched_on=fetched_on, source_file=source_file)
            if commit:
                cur.execute(f"RELEASE SAVEPOINT {sp}")
                in_sp = False
                conn.commit()
            else:
                cur.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                cur.execute(f"RELEASE SAVEPOINT {sp}")
                in_sp = False
        except (Exception, SystemExit) as e:
            if in_sp:
                cur.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                cur.execute(f"RELEASE SAVEPOINT {sp}")
            if commit:
                conn.rollback()
            msg = e.code if isinstance(e, SystemExit) else f"{type(e).__name__}: {e}"
            print(f"  {period}: FAILED, nothing stored for this month ({msg})")
            rest = list(periods[i + 1:])
            if rest:
                print(f"  not attempted: {', '.join(rest)}")
            _print_tally(tally)
            return 1
        if stats is not None:
            stats["periods"].append(period)
            stats["kinds"][period] = kind
            if kind in ("new", "revised"):
                stats["stored_rows"] += len(records)
            if kind in ("new", LIVE_MISSING):
                stats["live_rows"] += len(records)
        tally[kind][0] += 1
        # a count of changed areas for the summary line, not a source value
        tally[kind][1] += (cmp["changed"] if kind in ("new", "revised")
                           else 0)  # not a source value
        n_live = f"{len(records):,}"
        if kind == "new":
            action = ("; stored edition 1 and inserted " if write else
                      "; would store edition 1 and insert ") + f"{n_live} live rows"
        elif kind == LIVE_MISSING:
            action = ("; editions month missing from live: "
                      + ("inserted " if write else "would insert ")
                      + f"{n_live} live rows (no new edition)")
        else:
            action = ""
        print(f"  {period}: {kind}, {cmp['changed']} areas "
              f"{'new' if kind == 'new' else 'changed'} (compared with "
              f"{cmp['against']})"
              + ("; " + "; ".join(cmp["examples"]) if cmp["examples"] else "")
              + action + (" COMMITTED" if commit else ""))
    _print_tally(tally)
    return 0


def _print_tally(tally):
    print("  summary: " + ", ".join(
        f"{k} {n} month(s)/{c} areas" for k, (n, c) in tally.items()))


def held_periods(cur, profile: Profile, editions_exist: bool) -> list:
    """Periods held: in the live table, plus any in the editions table."""
    spec, pc = profile.spec, profile.spec.period_col
    cur.execute(f"SELECT DISTINCT {pc} FROM public.{spec.live_table}")
    held = {_p(r[0]) for r in cur.fetchall()}
    if editions_exist:
        cur.execute(f"SELECT DISTINCT {pc} FROM public.{spec.editions_table}")
        held |= {_p(r[0]) for r in cur.fetchall()}
    return sorted(held)


def log_run(cur, profile: Profile, rows_written: int, notes: str,
            started_at=None) -> None:
    """Write the pipeline_run_log row for a committed run (status 'success'
    is the only value the table accepts for new rows). source_number and
    source_code are both profile.run_source. Called only on committed runs;
    previews and --simulate never reach it."""
    cur.execute("""
        INSERT INTO pipeline_run_log
            (agent_name, source_number, source_code, rows_written, status,
             started_at, completed_at, notes)
        VALUES (%s, %s, %s, %s, 'success', COALESCE(%s, now()), now(), %s)
    """, (profile.run_agent, profile.run_source, profile.run_source,
          rows_written, started_at, notes))


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def status(cur, profile: Profile) -> dict:
    """editions_core.status under the key names the S1b, RO4 and S8b
    wrappers use ('drift' is 'drift_periods', 'forked' is 'chain_errors'),
    plus 'live_missing' (periods with editions but no live rows), which makes
    ok false. Every period is a string."""
    spec = profile.spec
    st = core.status(cur, spec)
    missing = live_missing_periods(cur, profile)
    return {"new_periods": [_p(x) for x in st["new_periods"]],
            "drift_periods": [_p(x) for x in st["drift"]],
            "pending_refresh": [_p(x) for x in st["pending_refresh"]],
            "chain_errors": {_p(k): v for k, v in st["forked"].items()},
            "bad_counts": {_p(k): v for k, v in st["bad_counts"].items()},
            "live_missing": missing,
            "periods": st["periods"],
            "ok": st["ok"] and not missing}


def format_status(profile: Profile, st: dict, extra_lines=()) -> str:
    """s1_editions.format_status under profile.heading, with extra_lines
    (a loader's own warnings) and a line per editions period missing from
    live inserted before the closing status line."""
    from s1_editions import format_status as _format_status
    text = _format_status(st, profile.heading)
    lines = list(extra_lines)
    lines += [f"  editions month missing from live: {mo} (run load --commit "
              "to insert its live rows)" for mo in st.get("live_missing") or []]
    if not lines:
        return text
    head, tail = text.rsplit("\n", 1)
    return "\n".join([head] + lines + [tail])


# ---------------------------------------------------------------------------
# Command scaffolding
# ---------------------------------------------------------------------------

def connect(writing: bool):
    """A writable connection, or a read-only one."""
    from _db import get_conn, get_readonly_conn
    return get_conn() if writing else get_readonly_conn()


def table_exists(cur, table: str) -> bool:
    cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
    return cur.fetchone()[0] is not None


def no_table(profile: Profile) -> str:
    return (f"{profile.spec.editions_table} does not exist yet; run "
            "`ddl --commit`, then `sync-new --commit`")


def finish(conn, args, done_msg) -> None:
    if args.commit:
        conn.commit()
        print(f"{done_msg}. COMMITTED")
    else:
        conn.rollback()
        print(f"SIMULATION: {done_msg}. ROLLED BACK (nothing persisted)")


def mode_parser(p) -> None:
    """The mutually exclusive --commit / --simulate group."""
    g = p.add_mutually_exclusive_group()
    g.add_argument("--commit", action="store_true",
                   help="write (append-only editions; irreversible)")
    g.add_argument("--simulate", action="store_true",
                   help="run the --commit path and always roll back")


def run_ddl(profile: Profile, args, *, connect=connect,
            table_exists=table_exists, create_schema=None) -> int:
    """ddl [--commit | --simulate]: the editions table and its triggers."""
    spec, table = profile.spec, profile.spec.editions_table
    writing = args.commit or args.simulate
    conn = connect(writing)
    try:
        with conn.cursor() as cur:
            exists = table_exists(cur, table)
            print(f"{table}: {'exists' if exists else 'does not exist'}")
            if not writing:
                print("DRY RUN: nothing written (use --commit or --simulate)")
                return 0
            if create_schema is None:
                core.create_schema(cur, spec)
            else:
                create_schema(cur)
        finish(conn, args, f"ddl: {table} and triggers {spec.trigger}, "
                           f"{spec.truncate_trigger} present")
        return 0
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def run_status(profile: Profile, args, *, connect=connect,
               table_exists=table_exists, status=None, format_status=None,
               status_notes=None) -> int:
    """status: what needs action; exit 1 if anything, or if the editions
    table does not exist (status_notes(cur) -> lines printed first then)."""
    status = status or (lambda cur: globals()["status"](cur, profile))
    format_status = format_status or (
        lambda st: globals()["format_status"](profile, st))
    conn = connect(False)
    try:
        with conn.cursor() as cur:
            if not table_exists(cur, profile.spec.editions_table):
                for line in (status_notes(cur) if status_notes else []):
                    print(line)
                print(f"status: {no_table(profile)}")
                return 1
            st = status(cur)
    finally:
        conn.close()
    print(format_status(st))
    return 0 if st["ok"] else 1


def run_sync_new(profile: Profile, args, *, connect=connect,
                 table_exists=table_exists, preflight=None, status=None,
                 format_status=None) -> int:
    """sync-new [--expected-authorities N] [--commit | --simulate]: edition 1
    'as loaded' for every live period with no editions; a run-log row on
    --commit (rolled back with everything else on --simulate).
    preflight(cur) halts on a loader's own stop condition."""
    spec, table, pc = profile.spec, profile.spec.editions_table, profile.spec.period_col
    status = status or (lambda cur: globals()["status"](cur, profile))
    format_status = format_status or (
        lambda st: globals()["format_status"](profile, st))
    writing = args.commit or args.simulate
    conn = connect(writing)
    try:
        with conn.cursor() as cur:
            if not table_exists(cur, table):
                halt(no_table(profile))
            if preflight is not None:
                preflight(cur)
            _, new, _ = core.latest_map(cur, spec)
            print("months with no editions: "
                  + (", ".join(_p(x) for x in new) or "none"))
            if not writing:
                print("DRY RUN: nothing written (use --commit or --simulate)")
                return 0
            # as the core returns them (a DATE column's dates stay dates in
            # SQL, so ANY(%s) below keeps its type); strings for messages
            done = core.sync_new(cur, spec, args.expected_authorities)
            for p in done:
                cur.execute(f"SELECT COUNT(*) FROM public.{table} "
                            f"WHERE {pc} = %s AND edition = 1", (p,))
                print(f"  {_p(p)}: edition 1 recorded, {cur.fetchone()[0]} rows")
            bad = (load_checks.check_latest_equals_live(cur, spec, done)
                   if done else [])
            st = status(cur)
            if st["new_periods"] or st["chain_errors"]:
                bad.append(format_status(st))
            if bad:
                halt("sync-new failed its checks, rolled back: "
                     + "; ".join(bad[:6]))
            if done:
                cur.execute(f"SELECT COUNT(*) FROM public.{table} "
                            f"WHERE {pc} = ANY(%s) AND edition = 1", (done,))
                n = cur.fetchone()[0]
                log_run(cur, profile, n, f"sync-new: edition 1 recorded 'as "
                        f"loaded' for {', '.join(_p(x) for x in done)} ({n} "
                        "rows) from the "
                        "live table; nothing fetched from the API.")
        finish(conn, args, f"{len(done)} month(s) recorded as edition 1")
        return 0
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _accept_periods(cur, spec, accept) -> tuple:
    """The --accept-drift strings as the database's own period values (a
    DATE column's '2026-04-01' becomes its date, which is what
    editions_core compares them with). Makes editions_core._plan's halts
    first, with the same wording but periods as strings: live periods with
    no editions, an invalid chain, and an accepted period that is not
    drifted (one matching no period included)."""
    tips, new, errors = core.latest_map(cur, spec)
    if new:
        halt(f"periods with no editions {[_p(x) for x in new]}; run sync-new "
             "first")
    if errors:
        halt("invalid edition chain: " + "; ".join(
            f"{p}: {m}" for p, m in errors.items()))
    drifted = {_p(p): p for p, tip in tips.items()
               if core.classify_period(cur, spec, p, tip)[0] == "drift"}
    stray = sorted(set(accept) - set(drifted))
    if stray:
        halt(f"--accept-drift {stray}: not drifted periods, nothing to accept")
    return tuple(drifted[a] for a in accept)


def _refresh_guard(cur, spec, accept) -> None:
    """editions_core.refresh_latest's two refusals before it writes
    (unaccepted drift; rows only on one side), made here first so the
    message names periods as strings; the wording is the core's."""
    plan, drift = core._plan(cur, spec, accept)
    if drift:
        halt("live differs from the latest edition and matches no stored "
             f"edition for {[_p(x) for x in drift]}: changed outside the "
             "editions tables. Load it as an edition, or re-run with "
             "--accept-drift PERIOD to overwrite it with the latest edition")
    stuck = core._unrepairable(plan)
    if stuck:
        halt(f"{[_p(x) for x in stuck]} differ from the latest edition in rows "
             "present in only one of them; an update of the refresh columns "
             "cannot repair that")


def run_refresh_latest(profile: Profile, args, *, connect=connect,
                       table_exists=table_exists, preflight=None) -> int:
    """refresh-latest [--commit | --simulate] [--accept-drift PERIOD]: copy
    each period's latest edition into the live table, under the core's
    before/after hash guard."""
    spec, table = profile.spec, profile.spec.editions_table
    writing = args.commit or args.simulate
    accept = tuple(args.accept_drift or ())
    conn = connect(writing)
    try:
        with conn.cursor() as cur:
            if not table_exists(cur, table):
                halt(no_table(profile))
            if preflight is not None:
                preflight(cur)
            accept = _accept_periods(cur, spec, accept)
            counts = core.refresh_counts(cur, spec, accept)
            print("rows refresh-latest would write: "
                  + (", ".join(f"{p}={n}" for p, n in counts.items()) or "none")
                  + f" (total {sum(counts.values())})")
            if not writing:
                print("DRY RUN: nothing written (use --commit or --simulate)")
                return 0
            _refresh_guard(cur, spec, accept)
            res = core.refresh_latest(cur, spec, accept)
            if res["updated"]:
                bad = load_checks.check_latest_equals_live(
                    cur, spec, sorted(res["updated"]))
                if bad:
                    halt("refresh-latest: live differs from the latest edition "
                         "after the refresh, rolled back: " + "; ".join(bad[:6]))
        finish(conn, args, f"{res['rows']} live rows refreshed in "
                           f"{sorted(_p(x) for x in res['updated'])}; "
                           "before/after guard "
                           "passed")
        return 0
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
