"""S8b (DWP Stat-Xplore Housing Benefit caseload by accommodation type):
edition history and loader on the shared editions core.

la_hb_accom_type_caseload holds one row per authority, month and
accommodation type (SA, TA, OTHER, UNKNOWN): the latest layer. This module
owns la_hb_accom_type_caseload_editions, which keeps every fetch of a month
that differed from the one before, so a DWP revision never overwrites what
was held. The editions machinery is editions_core driven by SPEC.

Blanks and zeros (docs/RULES.md rule 1): an API null is None, a returned 0 is
0, and a blank is never coerced to zero. A merged-area total is the sum of its
parts only if every part is present; otherwise it is None. Cells are read by
parse_cube, not blank_reader: the Stat-Xplore API returns JSON, each cell a
JSON integer or null (no text markers), and parse_cube is the stricter reader
for that (an int or None, anything else refused). Nothing needs the
database, the network or the API key at import; the Stat-Xplore client is
imported only when a fetch is made.

Run log: `sync-new --commit` and `load --commit` write one pipeline_run_log
row (agent 'Source 8b - HB Accommodation Type', source_number and source_code
'8b', status 'success') when the run succeeds, including a `load` that
rechecked and found nothing new, because the check is the run and the
registry's due date is computed from the last logged success. Previews and
--simulate persist nothing; a failed `load` logs nothing.

Subcommands (every writing command previews by default; --commit and
--simulate are mutually exclusive; --simulate runs the --commit path and
always rolls back):
    python scripts/s8b_hb_editions.py ddl [--commit | --simulate]
        # create the editions table and its append-only triggers; idempotent
    python scripts/s8b_hb_editions.py status
        # what needs action, including any editions month missing from
        # live; exit 1 if anything (or if the editions table does not exist
        # yet; it is never created here)
    python scripts/s8b_hb_editions.py sync-new [--expected-authorities N]
                                               [--commit | --simulate]
        # edition 1 'as loaded' for every live month with no editions
    python scripts/s8b_hb_editions.py load [--recheck-all] [--recheck-n N]
                                           [--months M ...]
                                           [--commit | --simulate]
        # months come from the API's date valueset: every available month
        # after the earliest held that is not held, plus the latest N held
        # months (6 by default; all with --recheck-all) as a revision check.
        # If nothing is held, --months must be given. Each month is fetched
        # (read-only API calls, also in preview), checked (296 areas x 4
        # types, never a short month) and compared with the month's latest
        # stored edition: new (edition 1 AND the month's live rows, in one
        # transaction, checked equal cell for cell), unchanged (nothing
        # stored) or revised (next edition, superseding the tip; live is
        # changed only by refresh-latest). A month with editions but no live
        # rows is always fetched; if its tip equals the fetch only its live
        # rows are inserted (no new edition). Each month is its own
        # transaction; a failure leaves earlier months committed, stops, and
        # exits non-zero. If the editions table does not exist yet the
        # preview compares with the LIVE table and says so; --commit and
        # --simulate need the table and every live month recorded (sync-new).
        # An unresolvable geography code is a hard stop.
    python scripts/s8b_hb_editions.py refresh-latest [--commit | --simulate]
                                                     [--accept-drift MONTH]
        # copy each month's latest edition into the live table (claimants
        # only), under the core's before/after hash guard
"""
import argparse
import hashlib
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import editions_core as core  # noqa: E402
import load_checks  # noqa: E402
from editions_core import halt  # noqa: E402

ACCOM_TYPES = ("SA", "TA", "OTHER", "UNKNOWN")

# Member ids as in the old loader (s8b_hb_accom_type_build.ACCOM_MEMBERS).
ACCOM_MEMBER_IDS = {
    "SA": "str:value:hb_new:V_F_HB_NEW:SATA:C_SATA:1",
    "TA": "str:value:hb_new:V_F_HB_NEW:SATA:C_SATA:2",
    "OTHER": "str:value:hb_new:V_F_HB_NEW:SATA:C_SATA:9",
    "UNKNOWN": "str:value:hb_new:V_F_HB_NEW:SATA:C_SATA:99",
}

_MONTH_RE = re.compile(r"(19|20)[0-9]{2}(0[1-9]|1[0-2])")


def parse_cube(resp: dict) -> dict:
    """Map source geography code to value (int or None) from a cube response."""
    cubes = resp["cubes"]
    values = cubes[list(cubes.keys())[0]]["values"]
    items = resp["fields"][0]["items"]
    if len(values) != len(items):
        raise ValueError(
            f"Cube has {len(values)} values but {len(items)} geography items")
    out = {}
    for i, item in enumerate(items):
        code = item["uris"][0].split(":")[-1]
        v = values[i]
        while isinstance(v, list):
            if not v:
                raise ValueError(f"Empty value list for geography {code}")
            v = v[0]
        if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
            raise ValueError(
                f"Non-integer value {v!r} for geography {code}; refusing to coerce")
        out[code] = v
    return out


def build_records(raw, lad_to_uris, month, accom_type):
    """One record per canonical lad24cd; total is None unless every part is present."""
    records = []
    for lad, uris in sorted(lad_to_uris.items()):
        parts = [raw.get(u.split(":")[-1]) for u in uris]
        if parts and all(p is not None for p in parts):
            total = sum(parts)
        else:
            total = None
        records.append({"lad24cd": lad, "month": month,
                        "accom_type": accom_type, "claimants": total})
    return records


def month_from_member(member_id):
    """Last colon segment as yyyymm (six digits), else None."""
    last = str(member_id).split(":")[-1]
    return last if _MONTH_RE.fullmatch(last) else None


def available_months(date_members):
    months = {month_from_member(d.get("id", "")) for d in date_members}
    months.discard(None)
    return sorted(months)


def plan_months(held, available, recheck_n=6, recheck_all=False):
    """Return (new, recheck), both ascending and never overlapping.

    new: every available month that is not held and is later than the
    EARLIEST held month (gaps inside the held range are loaded; history before
    the first held month is never auto-loaded). If nothing is held, new is
    empty: the loader must require an explicit --months list.
    recheck: the latest recheck_n held months that are still available (all of
    them when recheck_all).
    """
    avail = sorted(set(available))
    earliest = min(held) if held else None
    new = [a for a in avail if earliest is not None and a > earliest and a not in held]
    still = [a for a in avail if a in held]
    if recheck_all:
        recheck = still
    elif recheck_n > 0:
        recheck = still[-recheck_n:]
    else:
        recheck = []
    return new, recheck


def content_sha256(records):
    """SHA-256 of the records; meant for one month per call (month is still
    part of the sort key and the serialisation, so mixed input stays stable)."""
    rows = sorted(records, key=lambda r: (r["lad24cd"], r["accom_type"], r["month"]))
    lines = []
    for r in rows:
        c = r["claimants"]
        if c is not None and (isinstance(c, bool) or not isinstance(c, int)):
            raise ValueError(
                f"claimants must be an int or None, got {c!r} for {r['lad24cd']}")
        lines.append("|".join([r["lad24cd"], r["accom_type"], r["month"],
                               "NULL" if c is None else str(c)]))
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------

LIVE = "la_hb_accom_type_caseload"
TABLE = "la_hb_accom_type_caseload_editions"
# Every English authority in la_boundaries; a month with fewer areas in any
# accommodation type is a short fetch and is refused.
EXPECTED_AREAS = 296


def key_types_for(table: str) -> tuple:
    """Key column types of an S8b editions table: month six digits, accom_type
    one of ACCOM_TYPES (the live table has text for both, with no check)."""
    types = ", ".join(f"'{t}'" for t in ACCOM_TYPES)
    return (
        ("month", f"varchar(6) NOT NULL CONSTRAINT {table}_month_chk "
                  "CHECK (month ~ '^[0-9]{6}$')"),
        ("accom_type", f"varchar(10) NOT NULL CONSTRAINT {table}_accom_type_chk "
                       f"CHECK (accom_type IN ({types}))"),
    )


# Live table (checked 2026-10-08): lad24cd, month, accom_type text NOT NULL,
# claimants integer (nullable), loaded_at timestamptz DEFAULT now(), primary
# key (lad24cd, month, accom_type). The editions table matches it with tighter
# key types and the core's lad24cd -> la_boundaries foreign key.
SPEC = core.EditionSpec(
    name="s8b",
    live_table=LIVE,
    editions_table=TABLE,
    key_cols=("lad24cd", "accom_type"),
    period_col="month",
    value_cols=(("claimants", "integer"),),
    refresh_cols=("claimants",),
    key_types=key_types_for(TABLE),
)

RELEASE_LABEL = "stat-xplore fetch {}"
SOURCE_FILE = "Stat-Xplore table query hb_new by accommodation type"


def create_schema(cur, spec: core.EditionSpec = SPEC) -> None:
    core.create_schema(cur, spec)


def table_exists(cur, table: str) -> bool:
    cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
    return cur.fetchone()[0] is not None


# ---------------------------------------------------------------------------
# Stat-Xplore: geography, months, fetch (moved from s8b_hb_accom_type_build)
# ---------------------------------------------------------------------------

DB_ID = "str:database:hb_new"
MEASURE_ID = "str:count:hb_new:V_F_HB_NEW"
GEO_FIELD_ID = "str:field:hb_new:V_F_HB_NEW:ADMIN_LA_CODE"
GEO_VALUESET_ID = "str:valueset:hb_new:V_F_HB_NEW:ADMIN_LA_CODE:V_C_ADMIN_LA"
DATE_FIELD_ID = "str:field:hb_new:F_HB_NEW_DATE:NEW_DATE_NAME"
DATE_VALUESET_ID = "str:valueset:hb_new:F_HB_NEW_DATE:NEW_DATE_NAME:C_HB_NEW_DATE"
MONTH_MEMBER_PREFIX = "str:value:hb_new:F_HB_NEW_DATE:NEW_DATE_NAME:C_HB_NEW_DATE:"
SATA_FIELD_ID = "str:field:hb_new:V_F_HB_NEW:SATA"
BATCH_SIZE = 50
BATCH_PAUSE_S = 2


def get_english_la_members():
    """English LA members of the admin geography valueset."""
    from statxplore_client import api_get_all_pages
    print("  Fetching geography members...")
    members = api_get_all_pages(f"/schema/{GEO_VALUESET_ID}")
    english = [m for m in members if m["id"].split(":")[-1].startswith("E")]
    print(f"  Total: {len(members)}, English: {len(english)}")
    return english


def resolve_geography(english_members, conn):
    """Map API geography codes to current LAD24CD, handling historical
    mergers (la_boundaries directly, else la_code_lookup old -> new). An
    unresolvable code is a hard stop (load_checks.check_codes: UNEXPLAINED
    until explained), not the old loader's warning."""
    code_to_uri = {}
    for m in english_members:
        uri = m["id"]
        code_to_uri[uri.split(":")[-1]] = uri

    cur = conn.cursor()
    cur.execute("SELECT lad24cd FROM la_boundaries")
    current_lads = {r[0] for r in cur.fetchall()}
    cur.execute("SELECT old_code, new_code FROM la_code_lookup")
    code_lookup = {}
    for old, new in cur.fetchall():
        code_lookup.setdefault(old, []).append(new)

    lad_to_uris = {}
    unresolvable = []
    for code, uri in code_to_uri.items():
        if code in current_lads:
            lad_to_uris.setdefault(code, []).append(uri)
        elif code in code_lookup:
            targets = [t for t in code_lookup[code] if t in current_lads]
            if targets:
                for t in targets:
                    lad_to_uris.setdefault(t, []).append(uri)
            else:
                unresolvable.append(code)
        else:
            unresolvable.append(code)

    problems = load_checks.check_codes(cur, list(code_to_uri))
    problems += [f"UNEXPLAINED {c}" for c in sorted(unresolvable)
                 if f"UNEXPLAINED {c}" not in problems]
    if problems:
        halt(f"unresolvable Stat-Xplore geography codes: {problems}; explain "
             "them in la_code_lookup before loading")
    print(f"  Resolved to {len(lad_to_uris)} current LAD24CD codes")
    return lad_to_uris, list(code_to_uri.values())


def get_available_months() -> list:
    """Months (yyyymm, ascending) in the API's date valueset."""
    from statxplore_client import api_get_all_pages
    return available_months(api_get_all_pages(f"/schema/{DATE_VALUESET_ID}"))


def fetch_month_accom(month_id, accom_member_id, query_uris) -> dict:
    """One month x one accommodation type, batched by geography (50 areas a
    query, 2 s between batches). {source code: int | None} via parse_cube."""
    from statxplore_client import api_post
    batches = [query_uris[i:i + BATCH_SIZE]
               for i in range(0, len(query_uris), BATCH_SIZE)]
    out = {}
    for batch_num, batch_uris in enumerate(batches):
        q = {
            "database": DB_ID,
            "measures": [MEASURE_ID],
            "recodes": {
                GEO_FIELD_ID: {"map": [[u] for u in batch_uris], "total": False},
                DATE_FIELD_ID: {"map": [[month_id]], "total": False},
                SATA_FIELD_ID: {"map": [[accom_member_id]], "total": False},
            },
            "dimensions": [[GEO_FIELD_ID], [DATE_FIELD_ID], [SATA_FIELD_ID]],
        }
        if batch_num > 0:
            time.sleep(BATCH_PAUSE_S)
        part = parse_cube(api_post("/table", q))
        dup = sorted(set(part) & set(out))
        if dup:
            raise ValueError(f"geography codes returned twice: {dup[:3]}")
        out.update(part)
    return out


def check_month_records(records: list, month: str) -> None:
    """ValueError unless the month is whole: every record of `month`, each
    of the four accommodation types present with at least EXPECTED_AREAS
    distinct areas, no key repeated, claimants an int or None."""
    by_type, seen = {}, set()
    for r in records:
        if r.get("month") != month:
            raise ValueError(f"{month}: a record of month {r.get('month')!r}")
        c = r.get("claimants")
        if c is not None and (isinstance(c, bool) or not isinstance(c, int)):
            raise ValueError(f"{month}: claimants {c!r} for {r.get('lad24cd')}")
        key = (r["lad24cd"], r["accom_type"])
        if key in seen:
            raise ValueError(f"{month}: {key} appears twice")
        seen.add(key)
        by_type.setdefault(r["accom_type"], set()).add(r["lad24cd"])
    stray = sorted(set(by_type) - set(ACCOM_TYPES))
    if stray:
        raise ValueError(f"{month}: unknown accommodation type(s) {stray}")
    for t in ACCOM_TYPES:
        n = len(by_type.get(t, ()))
        if n < EXPECTED_AREAS:
            raise ValueError(f"{month} {t}: {n} areas, expected "
                             f"{EXPECTED_AREAS}; a short month is never loaded")


def fetch_month(month: str, lad_to_uris, query_uris) -> list:
    """All four accommodation types of one month; records (build_records).
    Raises if any queried area is absent from a response, or if the result
    has fewer than EXPECTED_AREAS areas or four types: never a short month."""
    if not _MONTH_RE.fullmatch(month):
        raise ValueError(f"not a yyyymm month: {month!r}")
    queried = {u.split(":")[-1] for u in query_uris}
    records = []
    for t in ACCOM_TYPES:
        raw = fetch_month_accom(MONTH_MEMBER_PREFIX + month,
                                ACCOM_MEMBER_IDS[t], query_uris)
        missing = sorted(queried - set(raw))
        if missing:
            raise ValueError(f"{month} {t}: the API returned no item for "
                             f"{len(missing)} of {len(queried)} areas "
                             f"{missing[:3]}")
        records += build_records(raw, lad_to_uris, month, t)
    check_month_records(records, month)
    return records


# ---------------------------------------------------------------------------
# Compare and store
# ---------------------------------------------------------------------------

def _stored(cur, spec, month: str, against: str):
    """({(lad24cd, accom_type): claimants}, label) of what the month is
    compared with: its latest stored edition, or (against='live') the live
    table. Empty dict if the month has none. Halts on a broken chain."""
    if against == "live":
        cur.execute(f"SELECT lad24cd, accom_type, claimants FROM "
                    f"public.{spec.live_table} WHERE month = %s", (month,))
        return {(a, b): c for a, b, c in cur.fetchall()}, "live"
    if against != "editions":
        raise ValueError(f"against must be 'editions' or 'live', not {against!r}")
    try:
        tip = core.chain_tip(cur, spec, month)
    except LookupError:
        return {}, "no edition"
    except ValueError as e:
        halt(f"{spec.editions_table}: {e}")
    cur.execute(f"SELECT lad24cd, accom_type, claimants FROM "
                f"public.{spec.editions_table} WHERE month = %s AND edition = %s",
                (month, tip))
    return {(a, b): c for a, b, c in cur.fetchall()}, f"edition {tip}"


LIVE_MISSING = "live-missing"


def live_row_count(cur, spec, month: str) -> int:
    cur.execute(f"SELECT COUNT(*) FROM public.{spec.live_table} "
                "WHERE month = %s", (month,))
    return cur.fetchone()[0]


def live_missing_months(cur, spec) -> list:
    """Months with editions but no live rows (stranded: before the fix of
    2026-10-08 a new month was stored as edition 1 only and never reached the
    live table). Ascending."""
    cur.execute(f"SELECT DISTINCT e.month FROM public.{spec.editions_table} e "
                f"WHERE NOT EXISTS (SELECT 1 FROM public.{spec.live_table} l "
                "WHERE l.month = e.month) ORDER BY 1")
    return [r[0] for r in cur.fetchall()]


def insert_live(cur, spec, month: str, records: list) -> None:
    """The month's live rows (loaded_at takes its default, now())."""
    from psycopg2.extras import execute_values
    execute_values(cur, f"INSERT INTO public.{spec.live_table} "
                   "(lad24cd, month, accom_type, claimants) VALUES %s",
                   [(r["lad24cd"], month, r["accom_type"], r["claimants"])
                    for r in records], page_size=1000)


def check_live_equals_edition(cur, spec, month: str, edition: int) -> list:
    """Problems unless the month's live rows equal the edition cell for cell
    (EXCEPT ALL both ways, NULL equals NULL, NULL differs from 0) and hold
    EXPECTED_AREAS areas in each of the four accommodation types."""
    bad = []
    n = core.rows_differing(cur, spec, month, edition)
    if n:
        bad.append(f"{month}: {n} live rows differ from edition {edition}")
    cur.execute(f"SELECT accom_type, COUNT(DISTINCT lad24cd), COUNT(*) "
                f"FROM public.{spec.live_table} WHERE month = %s GROUP BY 1",
                (month,))
    got = {t: (a, r) for t, a, r in cur.fetchall()}
    want = {t: (EXPECTED_AREAS, EXPECTED_AREAS) for t in ACCOM_TYPES}
    if got != want:
        bad.append(f"{month}: live (areas, rows) per type {got}, expected "
                   f"{EXPECTED_AREAS} x {len(ACCOM_TYPES)} types")
    return bad


def compare_month(cur, spec, month: str, records: list,
                  against: str = "editions") -> dict:
    """{kind, changed, examples, against}: kind 'new' (nothing stored),
    'unchanged' or 'revised'; changed counts cells that differ (a key on one
    side only counts; NULL equals NULL, NULL differs from 0); up to three
    examples. Against the editions, an unchanged month with no live rows is
    LIVE_MISSING: its tip equals the fetch but it never reached the live
    table, so load inserts the live rows (no new edition)."""
    old, label = _stored(cur, spec, month, against)
    new = {(r["lad24cd"], r["accom_type"]): r["claimants"] for r in records}
    if not old:
        return {"kind": "new", "changed": len(new), "examples": [],
                "against": label}
    diff = [k for k in sorted(set(old) | set(new))
            if k not in old or k not in new or old[k] != new[k]]
    ex = [f"{k[0]} {k[1]}: {old.get(k, 'absent')} -> {new.get(k, 'absent')}"
          for k in diff[:3]]
    kind = "revised" if diff else "unchanged"
    if (kind == "unchanged" and against == "editions"
            and live_row_count(cur, spec, month) == 0):
        kind = LIVE_MISSING
    return {"kind": kind, "changed": len(diff), "examples": ex,
            "against": label}


def classify_month(cur, spec, month: str, records: list) -> str:
    """'new', 'unchanged', 'revised' or LIVE_MISSING against the month's
    latest stored edition."""
    return compare_month(cur, spec, month, records)["kind"]


def _live_into(cur, spec, month: str, records: list, edition: int) -> None:
    """Insert the month's live rows unless live already holds the month,
    then halt unless live equals `edition` and is 296 areas x 4 types."""
    if live_row_count(cur, spec, month) == 0:
        insert_live(cur, spec, month, records)
    bad = check_live_equals_edition(cur, spec, month, edition)
    if bad:
        halt(f"{month}: the live rows failed their checks against edition "
             f"{edition}, month rolled back: " + "; ".join(bad))


def apply_month(cur, spec, month: str, records: list, *,
                fetched_on: date) -> str:
    """Classify, then store: edition 1 (supersedes None) AND the month's
    live rows when new; the next edition superseding the tip when revised
    (live is left alone: a revision reaches live only through
    refresh-latest); the live rows only (no edition) when LIVE_MISSING;
    nothing when unchanged. Checks the stored edition (tip, row count,
    distinct keys) and, for new and LIVE_MISSING, that live equals the
    edition cell for cell with 296 areas x 4 types, before returning; any
    failure halts so the caller's per-month savepoint rolls the whole month
    back. Never commits. A return to the content of an older, non-tip
    edition (DWP publishes A, then B, then A again) is a revision against
    the tip (RULES 2.1) and is stored as a new edition:
    insert_edition(allow_revert=True). For a new month whose live rows
    already exist (not reachable from `load --commit`, which requires
    sync-new first) nothing is inserted into live, but live must still equal
    edition 1."""
    kind = classify_month(cur, spec, month, records)
    if kind == "unchanged":
        return kind
    if kind == LIVE_MISSING:
        _live_into(cur, spec, month, records, core.chain_tip(cur, spec, month))
        return kind
    sha = content_sha256(records)
    tip = None if kind == "new" else core.chain_tip(cur, spec, month)
    ed = core.insert_edition(
        cur, spec, records, month,
        release_label=RELEASE_LABEL.format(fetched_on.isoformat()),
        published_date=fetched_on, source_file=SOURCE_FILE,
        source_sha256=sha, supersedes=tip, strict=True, allow_revert=True)
    bad = load_checks.check_coverage(cur, spec, month, ed, len(records))
    if core.chain_tip(cur, spec, month) != ed:
        bad.append(f"{month}: edition {ed} is not the chain tip")
    if bad:
        halt(f"{month} edition {ed} failed its checks: " + "; ".join(bad))
    if kind == "new":
        _live_into(cur, spec, month, records, ed)
    return kind


def load_months(cur, spec, months, fetch, fetched_on: date, commit: bool, *,
                simulate: bool = False, against: str = "editions",
                stats: dict | None = None) -> int:
    """Fetch, check and compare each month; store it when commit or
    simulate. Each month runs in its own savepoint: on commit the month is
    committed on its own; on simulate, and in preview (neither flag: compare
    only, nothing applied), the savepoint is always rolled back. A failure
    (fetch, short month, check, halt) rolls that month back, stops, and
    returns 1; earlier committed months stay. Returns 0 when every month
    went through. fetch(month) -> records is injected (no network in tests).
    If stats is given it is filled for the run log: months (those gone
    through), kinds ({month: kind}), stored_rows (rows in the editions
    stored, 0 for unchanged months) and live_rows (live rows inserted: a new
    month's, in the same savepoint as its edition 1, and a LIVE_MISSING
    month's)."""
    if commit and simulate:
        raise ValueError("commit and simulate are mutually exclusive")
    write = commit or simulate
    conn = cur.connection
    tally = {"new": [0, 0], "unchanged": [0, 0], "revised": [0, 0],
             LIVE_MISSING: [0, 0]}
    if stats is not None:
        stats.update(months=[], kinds={}, stored_rows=0, live_rows=0)
    for i, month in enumerate(months):
        in_sp = False
        try:
            records = fetch(month)
            check_month_records(records, month)
            cur.execute("SAVEPOINT s8b_month")
            in_sp = True
            cmp = compare_month(cur, spec, month, records, against)
            kind = cmp["kind"]
            if write:
                kind = apply_month(cur, spec, month, records,
                                   fetched_on=fetched_on)
            if commit:
                cur.execute("RELEASE SAVEPOINT s8b_month")
                in_sp = False
                conn.commit()
            else:
                cur.execute("ROLLBACK TO SAVEPOINT s8b_month")
                cur.execute("RELEASE SAVEPOINT s8b_month")
                in_sp = False
        except (Exception, SystemExit) as e:
            if in_sp:
                cur.execute("ROLLBACK TO SAVEPOINT s8b_month")
                cur.execute("RELEASE SAVEPOINT s8b_month")
            if commit:
                conn.rollback()
            msg = e.code if isinstance(e, SystemExit) else f"{type(e).__name__}: {e}"
            print(f"  {month}: FAILED, nothing stored for this month ({msg})")
            rest = list(months[i + 1:])
            if rest:
                print(f"  not attempted: {', '.join(rest)}")
            _print_tally(tally)
            return 1
        if stats is not None:
            stats["months"].append(month)
            stats["kinds"][month] = kind
            if kind in ("new", "revised"):
                stats["stored_rows"] += len(records)
            if kind in ("new", LIVE_MISSING):
                stats["live_rows"] += len(records)
        tally[kind][0] += 1
        # a count of changed cells for the summary line, not a source value
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
        print(f"  {month}: {kind}, {cmp['changed']} cells "
              f"{'new' if kind == 'new' else 'changed'} (compared with "
              f"{cmp['against']})"
              + ("; " + "; ".join(cmp["examples"]) if cmp["examples"] else "")
              + action + (" COMMITTED" if commit else ""))
    _print_tally(tally)
    return 0


def _print_tally(tally):
    print("  summary: " + ", ".join(
        f"{k} {n} month(s)/{c} cells" for k, (n, c) in tally.items()))


RUN_AGENT = "Source 8b - HB Accommodation Type"
RUN_SOURCE = "8b"


def log_run(cur, rows_written: int, notes: str, started_at=None) -> None:
    """Write the pipeline_run_log row for a committed run (status 'success'
    is the only value the table accepts for new rows). source_number and
    source_code are both '8b', so vw_source_due attributes the run to S8b.
    Called only on `sync-new --commit` and `load --commit`; previews and
    --simulate never reach it."""
    cur.execute("""
        INSERT INTO pipeline_run_log
            (agent_name, source_number, source_code, rows_written, status,
             started_at, completed_at, notes)
        VALUES (%s, %s, %s, %s, 'success', COALESCE(%s, now()), now(), %s)
    """, (RUN_AGENT, RUN_SOURCE, RUN_SOURCE, rows_written, started_at, notes))


def load_run_notes(stats: dict, available: list, held: list) -> str:
    """Words for the run log: what was checked, what was stored."""
    kinds = stats["kinds"]
    by = {k: [mo for mo in stats["months"] if kinds[mo] == k]
          for k in ("new", "revised", "unchanged", LIVE_MISSING)}
    stored = by["new"] + by["revised"]
    head = ("Stored: " + "; ".join(f"{k} {', '.join(by[k])}"
                                   for k in ("new", "revised") if by[k])
            if stored else "Nothing new: no new month and no revision")
    if by[LIVE_MISSING]:
        head += (". Live rows inserted (no new edition) for editions months "
                 f"missing from live: {', '.join(by[LIVE_MISSING])}")
    live_rows = stats.get("live_rows", 0)  # not a source value
    return (f"{head}. Checked {len(stats['months'])} month(s) "
            f"({', '.join(stats['months']) or 'none'}); unchanged "
            f"{len(by['unchanged'])}. Held latest {max(held) if held else '-'}"
            f"; API latest {max(available) if available else '-'}. "
            f"Categories: SA, TA, OTHER, UNKNOWN. Rows stored: "
            f"{stats['stored_rows']} edition rows, {live_rows} live rows "
            "inserted. A run that finds nothing new is logged "
            "because the check is the run.")


def held_months(cur, spec, editions_exist: bool) -> list:
    """Months held: in the live table, plus any in the editions table."""
    cur.execute(f"SELECT DISTINCT month FROM public.{spec.live_table}")
    held = {r[0] for r in cur.fetchall()}
    if editions_exist:
        cur.execute(f"SELECT DISTINCT month FROM public.{spec.editions_table}")
        held |= {r[0] for r in cur.fetchall()}
    return sorted(held)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def status(cur, spec=SPEC) -> dict:
    """editions_core.status with SPEC under the key names the S1b and RO4
    wrappers use: 'drift' is 'drift_periods', 'forked' is 'chain_errors'.
    The core looks only at live months, so 'live_missing' adds the months
    with editions but no live rows (live_missing_months); any makes ok
    false."""
    st = core.status(cur, spec)
    missing = live_missing_months(cur, spec)
    return {"new_periods": st["new_periods"], "drift_periods": st["drift"],
            "pending_refresh": st["pending_refresh"],
            "chain_errors": st["forked"], "bad_counts": st["bad_counts"],
            "live_missing": missing,
            "periods": st["periods"], "ok": st["ok"] and not missing}


def format_status(st: dict) -> str:
    from s1_editions import format_status as _format_status
    text = _format_status(st, "S8b HB caseload by accommodation type").replace(
        "periods in the live table", "months in the live table")
    missing = st.get("live_missing") or []
    if not missing:
        return text
    head, tail = text.rsplit("\n", 1)
    lines = [f"  editions month missing from live: {mo} (run load --commit "
             "to insert its live rows)" for mo in missing]
    return "\n".join([head] + lines + [tail])


def _no_table():
    return (f"{TABLE} does not exist yet; run `ddl --commit`, then "
            "`sync-new --commit`")


def _conn(writing):
    from _db import get_conn, get_readonly_conn
    return get_conn() if writing else get_readonly_conn()


def _finish(conn, args, done_msg):
    if args.commit:
        conn.commit()
        print(f"{done_msg}. COMMITTED")
    else:
        conn.rollback()
        print(f"SIMULATION: {done_msg}. ROLLED BACK (nothing persisted)")


def cmd_ddl(args) -> int:
    writing = args.commit or args.simulate
    conn = _conn(writing)
    try:
        with conn.cursor() as cur:
            exists = table_exists(cur, TABLE)
            print(f"{TABLE}: {'exists' if exists else 'does not exist'}")
            if not writing:
                print("DRY RUN: nothing written (use --commit or --simulate)")
                return 0
            create_schema(cur)
        _finish(conn, args, f"ddl: {TABLE} and triggers {SPEC.trigger}, "
                            f"{SPEC.truncate_trigger} present")
        return 0
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def cmd_status(_args) -> int:
    conn = _conn(False)
    try:
        with conn.cursor() as cur:
            if not table_exists(cur, TABLE):
                print(f"status: {_no_table()}")
                return 1
            st = status(cur)
    finally:
        conn.close()
    print(format_status(st))
    return 0 if st["ok"] else 1


def cmd_sync_new(args) -> int:
    writing = args.commit or args.simulate
    conn = _conn(writing)
    try:
        with conn.cursor() as cur:
            if not table_exists(cur, TABLE):
                halt(_no_table())
            _, new, _ = core.latest_map(cur, SPEC)
            print("months with no editions: " + (", ".join(new) or "none"))
            if not writing:
                print("DRY RUN: nothing written (use --commit or --simulate)")
                return 0
            done = core.sync_new(cur, SPEC, args.expected_authorities)
            for p in done:
                cur.execute(f"SELECT COUNT(*) FROM public.{TABLE} "
                            "WHERE month = %s AND edition = 1", (p,))
                print(f"  {p}: edition 1 recorded, {cur.fetchone()[0]} rows")
            bad = (load_checks.check_latest_equals_live(cur, SPEC, done)
                   if done else [])
            st = status(cur)
            if st["new_periods"] or st["chain_errors"]:
                bad.append(format_status(st))
            if bad:
                halt("sync-new failed its checks, rolled back: "
                     + "; ".join(bad[:6]))
            if done:
                cur.execute(f"SELECT COUNT(*) FROM public.{TABLE} "
                            "WHERE month = ANY(%s) AND edition = 1", (done,))
                n = cur.fetchone()[0]
                log_run(cur, n, f"sync-new: edition 1 recorded 'as loaded' "
                        f"for {', '.join(done)} ({n} rows) from the live "
                        "table; nothing fetched from the API.")
        _finish(conn, args, f"{len(done)} month(s) recorded as edition 1")
        return 0
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def cmd_refresh_latest(args) -> int:
    writing = args.commit or args.simulate
    accept = tuple(args.accept_drift or ())
    conn = _conn(writing)
    try:
        with conn.cursor() as cur:
            if not table_exists(cur, TABLE):
                halt(_no_table())
            counts = core.refresh_counts(cur, SPEC, accept)
            print("rows refresh-latest would write: "
                  + (", ".join(f"{p}={n}" for p, n in counts.items()) or "none")
                  + f" (total {sum(counts.values())})")
            if not writing:
                print("DRY RUN: nothing written (use --commit or --simulate)")
                return 0
            res = core.refresh_latest(cur, SPEC, accept)
            if res["updated"]:
                bad = load_checks.check_latest_equals_live(
                    cur, SPEC, sorted(res["updated"]))
                if bad:
                    halt("refresh-latest: live differs from the latest edition "
                         "after the refresh, rolled back: " + "; ".join(bad[:6]))
        _finish(conn, args, f"{res['rows']} live rows refreshed in "
                            f"{sorted(res['updated'])}; before/after guard "
                            "passed")
        return 0
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def cmd_load(args) -> int:
    writing = args.commit or args.simulate
    conn = _conn(writing)
    try:
        with conn.cursor() as cur:
            has_ed = table_exists(cur, TABLE)
            if writing:
                if not has_ed:
                    halt(_no_table())
                _, new, errors = core.latest_map(cur, SPEC)
                if errors:
                    halt(f"invalid edition chain: {errors}")
                if new:
                    halt(f"live months with no editions {new}; run sync-new "
                         "first so the history starts from what is held")
            against = "editions" if has_ed else "live"
            if not has_ed:
                print(f"NOTE: {TABLE} does not exist yet; this preview "
                      f"compares each fetch with the LIVE table {LIVE}")
            held = held_months(cur, SPEC, has_ed)
            available = get_available_months()
            print(f"held months: {', '.join(held) or 'none'}")
            print("available from the API: "
                  + (f"{available[0]} .. {available[-1]}" if available else "-")
                  + f" ({len(available)} months)")
            if args.months:
                bad = [x for x in args.months if x not in available]
                if bad:
                    halt(f"--months {bad} not available from the API")
                months = sorted(set(args.months))
            else:
                if not held:
                    halt("nothing is held, so no month can be planned; give "
                         "--months M [M ...]")
                new_m, recheck = plan_months(held, available, args.recheck_n,
                                             args.recheck_all)
                # editions months with no live rows are always fetched, so
                # load can insert their live rows (the repair path)
                stranded = ([mo for mo in live_missing_months(cur, SPEC)
                             if mo in available] if has_ed else [])
                print(f"planned: new {', '.join(new_m) or 'none'}; recheck "
                      f"{', '.join(recheck) or 'none'}"
                      + (f"; editions month missing from live "
                         f"{', '.join(stranded)}" if stranded else ""))
                months = sorted(set(new_m) | set(recheck) | set(stranded))
            if not months:
                print("nothing to fetch")
                return 0
            members = get_english_la_members()
            lad_to_uris, query_uris = resolve_geography(members, conn)
            stats = {}
            started = datetime.now(timezone.utc)
            rc = load_months(cur, SPEC, months,
                             lambda mo: fetch_month(mo, lad_to_uris, query_uris),
                             date.today(), args.commit, simulate=args.simulate,
                             against=against, stats=stats)
            if args.commit:
                if rc == 0:
                    # Stored something, or rechecked and found nothing new:
                    # either way the check is the run, so it is logged.
                    log_run(cur, stats["stored_rows"]
                            + stats.get("live_rows", 0),  # not a source value
                            load_run_notes(stats, available, held), started)
                    conn.commit()
                    print("pipeline_run_log row written")
                else:
                    print("pipeline_run_log: no row written (the run failed; "
                          "months committed before the failure are in the "
                          "editions table)")
        conn.rollback()  # commit mode has committed month by month already
        if not writing:
            print("PREVIEW: nothing written (use --commit or --simulate)")
        elif args.simulate:
            print("SIMULATION: ROLLED BACK (nothing persisted)")
        return rc
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _mode(p):
    g = p.add_mutually_exclusive_group()
    g.add_argument("--commit", action="store_true",
                   help="write (append-only editions; irreversible)")
    g.add_argument("--simulate", action="store_true",
                   help="run the --commit path and always roll back")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="S8b HB caseload by accommodation "
                                 "type: editions loader")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ddl", help="create the editions table (preview by "
                       "default)")
    _mode(p)
    p.set_defaults(func=cmd_ddl)
    sub.add_parser("status", help="what needs action; exit 1 if anything"
                   ).set_defaults(func=cmd_status)
    p = sub.add_parser("sync-new", help="edition 1 'as loaded' for live months "
                       "with no editions (preview by default)")
    p.add_argument("--expected-authorities", type=int, metavar="N",
                   help="authorities per month; only when nothing can be "
                   "derived (no month has editions yet)")
    _mode(p)
    p.set_defaults(func=cmd_sync_new)
    p = sub.add_parser("load", help="fetch, compare and store months (preview "
                       "by default)")
    p.add_argument("--recheck-all", action="store_true",
                   help="recheck every held month still available")
    p.add_argument("--recheck-n", type=int, default=None, metavar="N",
                   help="recheck the latest N held months (default 6)")
    p.add_argument("--months", nargs="+", metavar="M",
                   help="fetch exactly these yyyymm months instead of planning")
    _mode(p)
    p.set_defaults(func=cmd_load)
    p = sub.add_parser("refresh-latest", help="copy each month's latest "
                       "edition into the live table (preview by default)")
    _mode(p)
    p.add_argument("--accept-drift", action="append", metavar="MONTH",
                   help="overwrite this month although its live rows equal no "
                   "stored edition (repeatable)")
    p.set_defaults(func=cmd_refresh_latest)
    args = ap.parse_args(argv)
    if args.cmd == "load":
        if args.months and (args.recheck_all or args.recheck_n is not None):
            print("WARNING: --months given; --recheck-all / --recheck-n are "
                  "ignored", file=sys.stderr)
        if args.recheck_n is None:
            args.recheck_n = 6
    return args.func(args)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
