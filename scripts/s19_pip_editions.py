"""S19 (DWP Stat-Xplore PIP claimants): edition history and loader on the
shared editions core, plus the one-off month relabel migration.

la_pip_claimants holds one row per authority and month (total PIP cases with
entitlement, and those with the enhanced daily living award): the latest
layer. This module owns la_pip_claimants_editions, which keeps every fetch of
a month that differed from the one before, so a DWP revision never
overwrites what was held. The editions machinery is editions_core driven by
SPEC. Importing this module needs no Stat-Xplore key, database or network;
the Stat-Xplore client is imported only when a call is made.

Months are stored as sortable 'yyyymm' keys (docs/RULES.md rule 8). The old
table held text labels such as 'Apr-26'; label_to_key and relabel_plan are
the one-off migration's mapping (migrate-months). Until it has run, `load`
stops (it never guesses which form a month is in) and `status` says so.

Blanks and zeros (docs/RULES.md rule 1): a value is the sum of its parts only
if every part is present (a part absent from the response counts as
missing), otherwise None. The DWP '..' disclosure marker arrives from the
API as null and is stored as NULL; a returned 0 stays 0. Never `or 0`.

Run log: `sync-new --commit` and `load --commit` write one pipeline_run_log
row (agent 'Source 19 - PIP Claimants', source_number and source_code '19',
status 'success') when the run succeeds, including a `load` that rechecked
and found nothing new. Previews and --simulate persist nothing; a failed
`load` logs nothing.

Subcommands (every writing command previews by default; --commit and
--simulate are mutually exclusive; --simulate runs the --commit path and
always rolls back):
    python scripts/s19_pip_editions.py ddl [--commit | --simulate]
        # create the editions table and its append-only triggers; idempotent
    python scripts/s19_pip_editions.py status
        # what needs action, including unmigrated month labels and any
        # editions month missing from live; exit 1 if anything (or if the
        # editions table does not exist yet; it is never created here)
    python scripts/s19_pip_editions.py migrate-months [--commit | --simulate]
        # the one-off relabel of the live table's months ('Apr-26' ->
        # '202604') under LOCK TABLE ... IN SHARE ROW EXCLUSIVE MODE; the
        # preview runs the relabel and its checks, prints them and rolls back
    python scripts/s19_pip_editions.py sync-new [--expected-authorities N]
                                                [--commit | --simulate]
        # edition 1 'as loaded' for every live month with no editions
    python scripts/s19_pip_editions.py load [--recheck-all] [--recheck-n N]
                                            [--months M ...]
                                            [--commit | --simulate]
        # months come from the API's live date valueset (never a cached
        # latest month): every available month after the earliest held that
        # is not held, plus the latest N held months (6 by default; all with
        # --recheck-all) as a revision check. If nothing is held, --months
        # must be given. Each month is fetched (read-only API calls, also in
        # preview), checked (296 areas, both measures, never a short month)
        # and compared with the month's latest stored edition: new (edition
        # 1 AND the month's live rows, in one transaction, checked equal
        # cell for cell), unchanged (nothing stored) or revised (next
        # edition; live is changed only by refresh-latest). A month with
        # editions but no live rows is always fetched; if its tip equals the
        # fetch only its live rows are inserted (no new edition). Each month
        # is its own transaction; a failure leaves earlier months committed,
        # stops, and exits non-zero. If the editions table does not exist
        # yet the preview compares with the LIVE table and says so.
    python scripts/s19_pip_editions.py refresh-latest [--commit | --simulate]
                                                      [--accept-drift MONTH]
        # copy each month's latest edition into the live table, under the
        # core's before/after hash guard
"""
import argparse
import hashlib
import json
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import editions_core as core  # noqa: E402
import load_checks  # noqa: E402
from editions_core import halt  # noqa: E402
from statxplore_months import (  # noqa: E402
    MONTH_RE as _MONTH_RE, available_months, month_from_member, parse_cube,
    plan_months)

LABEL_FORMAT = "%b-%y"

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_LABEL_RE = re.compile(r"([A-Z][a-z]{2})-([0-9]{2})")
_KEY_RE = re.compile(r"([0-9]{4})(0[1-9]|1[0-2])")


def label_to_key(label: str) -> str:
    """'Apr-26' -> '202604'. Only a capitalised English three-letter month,
    a hyphen and a two-digit year (20yy); anything else is a ValueError."""
    m = _LABEL_RE.fullmatch(label) if isinstance(label, str) else None
    if not m or m.group(1) not in _MONTHS:
        raise ValueError(f"not a month label like 'Apr-26': {label!r}")
    return f"20{m.group(2)}{_MONTHS.index(m.group(1)) + 1:02d}"


def key_to_label(key: str) -> str:
    """'202604' -> 'Apr-26' (a display label; never stored as the key)."""
    m = _KEY_RE.fullmatch(key) if isinstance(key, str) else None
    if not m or not m.group(1).startswith("20"):
        raise ValueError(f"not a yyyymm month key: {key!r}")
    return f"{_MONTHS[int(m.group(2)) - 1]}-{m.group(1)[2:]}"


def relabel_plan(held_labels: list) -> dict:
    """{label: key} for each distinct held label. ValueError if a label is
    invalid or two distinct labels map to the same key."""
    plan, owner = {}, {}
    for label in dict.fromkeys(held_labels):
        key = label_to_key(label)
        if key in owner:
            raise ValueError(f"labels {owner[key]!r} and {label!r} both map "
                             f"to {key}")
        owner[key] = label
        plan[label] = key
    return plan


def _sum_parts(raw: dict, codes: list):
    parts = [raw.get(c) for c in codes]
    if parts and all(p is not None for p in parts):
        return sum(parts)
    return None


def build_records(total_raw: dict, enhanced_raw: dict, lad_to_uris: dict,
                  month: str) -> list:
    """One record per canonical lad24cd, sorted; each measure independently
    the sum of its parts only if every part is present, else None. The
    geography code of a URI is its last ':' segment."""
    records = []
    for lad, uris in sorted(lad_to_uris.items()):
        codes = [u.split(":")[-1] for u in uris]
        records.append({"lad24cd": lad, "month": month,
                        "pip_total_claimants": _sum_parts(total_raw, codes),
                        "pip_enhanced_daily_living":
                            _sum_parts(enhanced_raw, codes)})
    return records


def _cell(r: dict, col: str) -> str:
    v = r[col]
    if v is None:
        return "NULL"
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError(f"{col} must be an int or None, got {v!r} for "
                         f"{r['lad24cd']}")
    return str(v)


def content_sha256(records: list) -> str:
    """SHA-256 of the records sorted by (lad24cd, month); each line
    lad24cd|month|total-or-NULL|enhanced-or-NULL."""
    rows = sorted(records, key=lambda r: (r["lad24cd"], r["month"]))
    lines = ["|".join([r["lad24cd"], r["month"],
                       _cell(r, "pip_total_claimants"),
                       _cell(r, "pip_enhanced_daily_living")]) for r in rows]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


# --- one-off migration: month labels -> 'yyyymm' keys in the live table ---

_KEY_FORM = re.compile(r"[0-9]{6}")
_MEASURES = ("pip_total_claimants", "pip_enhanced_daily_living")


def _state(cur, table: str, month=None) -> dict:
    """Row count, distinct months, per-month totals and null counts, and the
    per-area values of `month` (if given) of the live table."""
    cur.execute(f"SELECT COUNT(*) FROM public.{table}")
    rows = cur.fetchone()[0]
    cur.execute(f"""SELECT month, COUNT(*),
                           SUM(pip_total_claimants),
                           SUM(pip_enhanced_daily_living),
                           COUNT(*) - COUNT(pip_total_claimants),
                           COUNT(*) - COUNT(pip_enhanced_daily_living)
                    FROM public.{table} GROUP BY month ORDER BY month""")
    per_month = {r[0]: {"rows": r[1], "sum_total": r[2], "sum_enhanced": r[3],
                        "null_total": r[4], "null_enhanced": r[5]}
                 for r in cur.fetchall()}
    latest = None
    if month is not None:
        cur.execute(f"""SELECT lad24cd, pip_total_claimants,
                               pip_enhanced_daily_living
                        FROM public.{table} WHERE month = %s
                        ORDER BY lad24cd""", (month,))
        latest = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    return {"rows": rows, "months": sorted(per_month), "per_month": per_month,
            "latest": latest}


def _compare_states(before: dict, after: dict, plan: dict) -> list:
    """Differences between the before and after states, the months compared
    through the plan {label: key}; [] if none."""
    probs = []
    if before["rows"] != after["rows"]:
        probs.append(f"row count {before['rows']} -> {after['rows']}")
    if len(before["months"]) != len(after["months"]):
        probs.append(f"distinct months {len(before['months'])} -> "
                     f"{len(after['months'])}")
    for label, figs in before["per_month"].items():
        got = after["per_month"].get(plan[label])
        if got != figs:
            probs.append(f"{label} -> {plan[label]}: totals {figs} -> {got}")
    if before["latest"] != after["latest"]:
        probs.append("per-area values of the latest month differ")
    return probs


def migrate_months(cur, live_table: str = "la_pip_claimants", *,
                   commit: bool = False) -> dict:
    """Relabel the live table's months from labels ('Apr-26') to sortable
    'yyyymm' keys (docs/RULES.md rule 8), one UPDATE per label, touching only
    the month column.

    Runs inside the caller's transaction and never commits or rolls it back;
    `commit` only labels the report. The writes are made in a savepoint of
    their own: if a check fails the savepoint is rolled back and the function
    halts (SystemExit), leaving the caller's transaction as it was.

    Halts, changing nothing, on: an empty table, a mix of label and key
    forms, an invalid or ambiguous label. Returns {"status": "already
    migrated"} if every month is already a key. Otherwise checks that rows,
    distinct months, per-month totals and null counts, and the per-area
    values of the month the relabelled MAX(month) selects are identical
    before and after, and returns the report."""
    table = core._ident(live_table)
    cur.execute(f"SELECT DISTINCT month FROM public.{table} ORDER BY month")
    held = [r[0] for r in cur.fetchall()]
    if not held:
        core.halt(f"{table} holds no rows; nothing to migrate")
    keys = [h for h in held if _KEY_FORM.fullmatch(h)]
    if len(keys) == len(held):
        return {"status": "already migrated"}
    if keys:
        core.halt(f"{table} holds a mix of month forms: keys {keys} and "
                  f"labels {[h for h in held if h not in keys]}; refusing "
                  "to migrate")
    try:
        plan = relabel_plan(held)
    except ValueError as e:
        core.halt(f"{table}: {e}; nothing changed")
    latest_key = max(plan.values())
    latest_label = next(lb for lb, k in plan.items() if k == latest_key)

    before = _state(cur, table, latest_label)
    with core._own_savepoint(cur, "s19_migrate_months"):
        for label, key in plan.items():
            cur.execute(f"UPDATE public.{table} SET month = %s "
                        "WHERE month = %s", (key, label))
        cur.execute(f"SELECT MAX(month) FROM public.{table}")
        selected = cur.fetchone()[0]
        after = _state(cur, table, selected)
        probs = _compare_states(before, after, plan)
        if selected != latest_key:
            probs.append(f"MAX(month) selects {selected}, expected "
                         f"{latest_key}")
        if probs:
            core.halt(f"{table} month relabel failed its checks, rolled "
                      "back: " + "; ".join(probs))
    return {"status": "migrated", "table": table, "committed": commit,
            "mapping": dict(plan), "reverse": {k: lb for lb, k in plan.items()},
            "latest_label": latest_label, "latest_key": latest_key,
            "before": before, "after": after}



# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------

LIVE = "la_pip_claimants"
TABLE = "la_pip_claimants_editions"
# Every English authority in la_boundaries; a month with fewer areas is a
# short fetch and is refused.
EXPECTED_AREAS = 296


def key_types_for(table: str) -> tuple:
    """Key column types of an S19 editions table: month six digits (the
    live table has text, with no check)."""
    return (("month", f"varchar(6) NOT NULL CONSTRAINT {table}_month_chk "
                      "CHECK (month ~ '^[0-9]{6}$')"),)


# Live table (checked 2026-10-08): lad24cd text NOT NULL, month text NOT
# NULL, pip_total_claimants integer, pip_enhanced_daily_living integer (both
# nullable), loaded_at timestamptz NOT NULL DEFAULT now(), primary key
# (lad24cd, month). The editions table matches it with a tighter month type
# and the core's lad24cd -> la_boundaries foreign key.
SPEC = core.EditionSpec(
    name="s19",
    live_table=LIVE,
    editions_table=TABLE,
    key_cols=("lad24cd",),
    period_col="month",
    value_cols=(("pip_total_claimants", "integer"),
                ("pip_enhanced_daily_living", "integer")),
    refresh_cols=("pip_total_claimants", "pip_enhanced_daily_living"),
    key_types=key_types_for(TABLE),
)

RELEASE_LABEL = "stat-xplore fetch {}"
SOURCE_FILE = "Stat-Xplore table query PIP_Monthly_new by local authority"


def create_schema(cur, spec: core.EditionSpec = SPEC) -> None:
    core.create_schema(cur, spec)


def table_exists(cur, table: str) -> bool:
    cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
    return cur.fetchone()[0] is not None


def months_migrated(cur, spec: core.EditionSpec = SPEC) -> bool:
    """True when every month in the live table is a 'yyyymm' key (an empty
    table counts as migrated: there is nothing to relabel). False while any
    month is still a label such as 'Apr-26', mixed forms included."""
    cur.execute(f"SELECT COUNT(*) FROM (SELECT DISTINCT month FROM "
                f"public.{core._ident(spec.live_table)}) d "
                "WHERE month !~ '^[0-9]{6}$'")
    return cur.fetchone()[0] == 0


def _unmigrated() -> str:
    return (f"{LIVE} still holds month labels such as 'Apr-26': the months "
            "have not been migrated to yyyymm keys yet, so this stops rather "
            "than guess which form a month is in; run `migrate-months` "
            "(preview), then `migrate-months --commit`")


# ---------------------------------------------------------------------------
# Stat-Xplore: schema discovery, geography, months, fetch (moved from
# s19_pip_build; every API call goes through statxplore_client)
# ---------------------------------------------------------------------------

CACHE_DIR = Path(__file__).resolve().parent.parent / "s19_cache"
DISCOVERY_CACHE = CACHE_DIR / "discovery.json"
BATCH_SIZE = 15
BATCH_PAUSE_S = 3


def discover_schema():
    from statxplore_client import api_get, api_get_all_pages
    print("=== Phase 1: Schema Discovery ===")

    root, _ = api_get("/schema")
    pip_databases = []
    all_databases = []

    def walk_folders(node, depth=0):
        node_type = node.get("type", "")
        if node_type == "DATABASE":
            all_databases.append(node)
            combined = (node.get("label", "") + " " + node.get("id", "")).lower()
            if "pip" in combined or "personal independence" in combined:
                pip_databases.append(node)
            return
        for child_ref in node.get("children", []):
            child_id = child_ref if isinstance(child_ref, str) else child_ref.get("id", "")
            if child_id:
                child, _ = api_get(f"/schema/{child_id}")
                walk_folders(child, depth + 1)

    walk_folders(root)

    if not pip_databases:
        sys.exit("HARD STOP: No PIP database found in Stat-Xplore schema.")

    print(f"  Found {len(pip_databases)} PIP database(s):")
    alternatives = []
    selected = None
    for db in pip_databases:
        label = db.get("label", "")
        db_id = db.get("id", "")
        print(f"    - {label} ({db_id})")
        alternatives.append({"label": label, "id": db_id})
        lower = label.lower()
        if "cases with entitlement" in lower and "from" in lower:
            selected = db

    if not selected:
        for db in pip_databases:
            if "cases with entitlement" in db.get("label", "").lower():
                selected = db
                break
    if not selected:
        selected = pip_databases[0]

    print(f"  Selected: {selected['label']} ({selected['id']})")

    # Get database children
    db_schema, _ = api_get(f"/schema/{selected['id']}")
    children = db_schema.get("children", [])

    measure_id = None
    geo_group = None
    dl_field_ref = None
    date_field_ref = None
    all_fields_refs = []

    for child in children:
        ctype = child.get("type", "")
        clabel = child.get("label", "").lower()
        cid = child.get("id", "")

        if ctype == "COUNT":
            measure_id = cid
            measure_label = child.get("label", "")
        elif ctype == "GROUP" and "geography" in clabel:
            geo_group = child
        elif ctype == "FIELD":
            all_fields_refs.append(child)
            if "daily living" in clabel:
                dl_field_ref = child
            elif clabel in ("month", "quarter", "date", "period", "time"):
                date_field_ref = child

    if not measure_id:
        sys.exit("HARD STOP: No caseload measure (COUNT) found.")
    if not geo_group:
        sys.exit("HARD STOP: No geography group found.")
    if not dl_field_ref:
        sys.exit("HARD STOP: No daily living award field found.")
    if not date_field_ref:
        sys.exit("HARD STOP: No date field found.")

    print(f"  Measure: {measure_label} ({measure_id})")

    # Walk geography group to find LA field
    geo_detail, _ = api_get(f"/schema/{geo_group['id']}")
    geo_fields = geo_detail.get("children", [])
    la_field = None
    for gf in geo_fields:
        gf_label = gf.get("label", "").lower()
        if "la" in gf_label or "local authority" in gf_label:
            la_field = gf
            break
    if not la_field:
        la_field = geo_fields[0]

    print(f"  Geography field: {la_field['label']} ({la_field['id']})")

    # Get geography field valuesets — record labels but only fetch
    # members for the LA-level valueset (OA/LSOA/MSOA are too large).
    geo_field_detail, _ = api_get(f"/schema/{la_field['id']}")
    geo_valuesets = geo_field_detail.get("children", [])
    geo_valuesets_info = []
    la_valueset_id = None
    la_valueset_members = None

    for vs in geo_valuesets:
        vs_label = vs.get("label", "")
        vs_id = vs.get("id", "")
        geo_valuesets_info.append({"label": vs_label, "id": vs_id})
        print(f"    Valueset: {vs_label}")
        if "local authority" in vs_label.lower():
            la_valueset_id = vs_id
            la_valueset_members = api_get_all_pages(f"/schema/{vs_id}")
            geo_valuesets_info[-1]["member_count"] = len(la_valueset_members)
            print(f"      -> {len(la_valueset_members)} members (fetched)")

    if la_valueset_id is None:
        sys.exit("HARD STOP: No LA-level geography valueset found.")

    english_members = [
        m for m in la_valueset_members if m["id"].split(":")[-1].startswith("E")
    ]
    print(f"  LA valueset: {la_valueset_id} — {len(la_valueset_members)} total, {len(english_members)} English")

    # Daily living — find Enhanced
    dl_detail, _ = api_get(f"/schema/{dl_field_ref['id']}")
    dl_vs = dl_detail.get("children", [])
    enhanced_id = None
    dl_valueset_id = None
    for vs in dl_vs:
        if vs.get("type") == "VALUESET":
            dl_valueset_id = vs["id"]
            dl_members = api_get_all_pages(f"/schema/{vs['id']}")
            for m in dl_members:
                if "enhanced" in m.get("label", "").lower():
                    enhanced_id = m["id"]
                    print(f"  Enhanced DL: {m['label']} ({enhanced_id})")
                    break
            break

    if not enhanced_id:
        sys.exit("HARD STOP: No 'Enhanced' member found in daily living field.")

    # Date — latest month (recorded for reference only: the loader reads
    # the available months from the live valueset on every run)
    date_detail, _ = api_get(f"/schema/{date_field_ref['id']}")
    date_vs = date_detail.get("children", [])
    date_valueset_id = None
    latest_month_id = None
    latest_month_label = None
    date_member_count = 0
    for vs in date_vs:
        if vs.get("type") == "VALUESET":
            date_valueset_id = vs["id"]
            date_members = api_get_all_pages(f"/schema/{vs['id']}")
            date_member_count = len(date_members)
            if date_members:
                latest = date_members[-1]
                latest_month_id = latest["id"]
                latest_month_label = latest.get("label", "")
                print(f"  Latest month: {latest_month_label} ({latest_month_id})")
                print(f"  Date valueset: {date_member_count} periods")
            break

    if not latest_month_id:
        sys.exit("HARD STOP: No date members found.")

    discovery = {
        "database": {"label": selected["label"], "id": selected["id"]},
        "alternatives_considered": alternatives,
        "measure": {"label": measure_label, "id": measure_id},
        "geography_field_id": la_field["id"],
        "geography_field_label": la_field["label"],
        "geography_valuesets": geo_valuesets_info,
        "la_valueset_id": la_valueset_id,
        "la_english_members": english_members,
        "daily_living_field_id": dl_field_ref["id"],
        "daily_living_field_label": dl_field_ref.get("label", ""),
        "dl_valueset_id": dl_valueset_id,
        "enhanced_member_id": enhanced_id,
        "date_field_id": date_field_ref["id"],
        "date_field_label": date_field_ref.get("label", ""),
        "date_valueset_id": date_valueset_id,
        "date_member_count": date_member_count,
        "latest_month": {"label": latest_month_label, "id": latest_month_id},
    }

    # Cache discovery (s19_cache/ is git-ignored; made here, never at import)
    CACHE_DIR.mkdir(exist_ok=True)
    DISCOVERY_CACHE.write_text(
        json.dumps(discovery, indent=2), encoding="utf-8"
    )
    return discovery


def get_discovery() -> dict:
    """The schema discovery: from the s19_cache checkpoint if present (its
    ids only: its latest month is never trusted, the months come from the
    live date valueset each run), else discover_schema()."""
    if DISCOVERY_CACHE.exists():
        print("=== Phase 1: Schema Discovery (from checkpoint) ===")
        discovery = json.loads(DISCOVERY_CACHE.read_text(encoding="utf-8"))
        discovery.pop("date_members", None)
        print(f"  Database: {discovery['database']['label']}")
        print(f"  Measure: {discovery['measure']['id']}")
        print(f"  English LAs: {len(discovery['la_english_members'])}")
        print("  (the checkpoint's latest month is not used; months are read "
              "from the live date valueset)")
        return discovery
    return discover_schema()


def get_available_months(discovery: dict) -> list:
    """Months (yyyymm, ascending) in the live date valueset; the members are
    kept in discovery['date_members'] (in memory only) for fetch_month."""
    from statxplore_client import api_get_all_pages
    members = api_get_all_pages(f"/schema/{discovery['date_valueset_id']}")
    discovery["date_members"] = members
    return available_months(members)


def resolve_geography(discovery, conn):
    print("\n=== Phase 2: Geography Resolution ===")
    english_members = discovery["la_english_members"]

    code_to_uri = {}
    for m in english_members:
        uri = m["id"]
        code = uri.split(":")[-1]
        code_to_uri[code] = uri
    print(f"  Extracted {len(code_to_uri)} English codes")

    cur = conn.cursor()
    cur.execute("SELECT lad24cd FROM la_boundaries")
    current_lads = {r[0] for r in cur.fetchall()}

    cur.execute("SELECT old_code, new_code FROM la_code_lookup")
    code_lookup = {}
    for old, new in cur.fetchall():
        code_lookup.setdefault(old, []).append(new)

    direct = {}
    historical = {}
    unresolvable = []
    historical_sum_map = {}

    for code, uri in code_to_uri.items():
        if code in current_lads:
            direct[code] = uri
        elif code in code_lookup:
            targets = [t for t in code_lookup[code] if t in current_lads]
            if targets:
                for t in targets:
                    historical[code] = {"uri": uri, "target": t}
                    historical_sum_map.setdefault(t, []).append(code)
            else:
                unresolvable.append(code)
        else:
            unresolvable.append(code)

    if unresolvable:
        sys.exit(f"HARD STOP: Unresolvable codes: {unresolvable}")

    resolved_lads = set(direct.keys()) | {v["target"] for v in historical.values()}
    coverage_count = len(resolved_lads)
    coverage_pct = round(coverage_count / 296 * 100, 1)

    if coverage_pct >= 95:
        confidence = "High"
    elif coverage_pct >= 50:
        confidence = "Medium"
    else:
        confidence = "Low"

    sum_needed = {k: v for k, v in historical_sum_map.items() if len(v) > 1}

    print(f"  Direct: {len(direct)}, Historical: {len(historical)}")
    print(f"  Coverage: {coverage_count}/296 ({coverage_pct}%)")
    print(f"  Confidence: {confidence}")
    if sum_needed:
        print(f"  Summing needed for {len(sum_needed)} LAD(s)")

    # Map resolved LAD24CD -> list of source URIs
    lad_to_uris = {}
    for code, uri in direct.items():
        lad_to_uris.setdefault(code, []).append(uri)
    for code, info in historical.items():
        lad_to_uris.setdefault(info["target"], []).append(info["uri"])

    return {
        "direct_count": len(direct),
        "historical_count": len(historical),
        "coverage_count": coverage_count,
        "coverage_pct": coverage_pct,
        "confidence": confidence,
        "sum_needed": dict(sum_needed),
        "lad_to_uris": lad_to_uris,
        "all_query_uris": list(code_to_uri.values()),
    }


def _batched_fetch(discovery, query_uris, month_id, enhanced: bool) -> dict:
    """One measure of one month, batched by geography (15 areas a query, 3 s
    between batches, as the old loader). {source code: int | None} via
    parse_cube (a JSON null, the DWP '..' marker, is None; 0 stays 0)."""
    from statxplore_client import api_post
    geo_field_id = discovery["geography_field_id"]
    date_field_id = discovery["date_field_id"]
    dl_field_id = discovery["daily_living_field_id"]
    name = "enhanced_dl" if enhanced else "total"
    batches = [query_uris[i:i + BATCH_SIZE]
               for i in range(0, len(query_uris), BATCH_SIZE)]
    out = {}
    for batch_num, batch_uris in enumerate(batches):
        recodes = {
            geo_field_id: {"map": [[u] for u in batch_uris], "total": False},
            date_field_id: {"map": [[month_id]], "total": False},
        }
        dims = [[geo_field_id], [date_field_id]]
        if enhanced:
            recodes[dl_field_id] = {"map": [[discovery["enhanced_member_id"]]],
                                    "total": False}
            dims.append([dl_field_id])
        q = {"database": discovery["database"]["id"],
             "measures": [discovery["measure"]["id"]],
             "recodes": recodes, "dimensions": dims}
        if batch_num > 0:
            time.sleep(BATCH_PAUSE_S)
        print(f"    {name} batch {batch_num + 1}/{len(batches)} "
              f"({len(batch_uris)} LAs)...")
        part = parse_cube(api_post("/table", q))
        dup = sorted(set(part) & set(out))
        if dup:
            raise ValueError(f"geography codes returned twice: {dup[:3]}")
        out.update(part)
    return out


def check_month_records(records: list, month: str) -> None:
    """ValueError unless the month is whole: month a yyyymm key and every
    record of it, no area repeated, at least EXPECTED_AREAS areas, both
    measures present on every record as an int or None, and each measure
    holding a value in at least one area (a measure with none is missing
    for the month)."""
    if not isinstance(month, str) or not _MONTH_RE.fullmatch(month):
        raise ValueError(f"not a yyyymm month key: {month!r}")
    seen = set()
    valued = {c: 0 for c in _MEASURES}
    for r in records:
        if r.get("month") != month:
            raise ValueError(f"{month}: a record of month {r.get('month')!r}")
        lad = r.get("lad24cd")
        for c in _MEASURES:
            if c not in r:
                raise ValueError(f"{month}: measure {c} missing for {lad}")
            v = r[c]
            if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
                raise ValueError(f"{month}: {c} {v!r} for {lad}")
            if v is not None:
                valued[c] += 1
        if lad in seen:
            raise ValueError(f"{month}: {lad} appears twice")
        seen.add(lad)
    if len(seen) < EXPECTED_AREAS:
        raise ValueError(f"{month}: {len(seen)} areas, expected "
                         f"{EXPECTED_AREAS}; a short month is never loaded")
    for c, n in valued.items():
        if n == 0:
            raise ValueError(f"{month}: measure {c} has no value in any area; "
                             "a month with a measure missing is never loaded")


def fetch_month(month: str, discovery: dict, geo: dict) -> list:
    """Both measures of one month (two queries: total, and enhanced daily
    living via the daily-living recode); records via build_records. Raises
    if the month is not a yyyymm key or not in the date valueset, if any
    queried area is absent from either response, or if the result has fewer
    than EXPECTED_AREAS areas or a measure missing: never a short month.
    Writes no files."""
    if not isinstance(month, str) or not _MONTH_RE.fullmatch(month):
        raise ValueError(f"not a yyyymm month key: {month!r}")
    if "date_members" not in discovery:
        get_available_months(discovery)
    ids = [d["id"] for d in discovery["date_members"]
           if month_from_member(d.get("id", "")) == month]
    if len(ids) != 1:
        raise ValueError(f"{month}: {len(ids)} members of the date valueset "
                         "end in this month, expected 1")
    query_uris = geo["all_query_uris"]
    queried = {u.split(":")[-1] for u in query_uris}
    print(f"  {month}: fetching total caseload...")
    total_raw = _batched_fetch(discovery, query_uris, ids[0], enhanced=False)
    print(f"  {month}: fetching enhanced daily living...")
    enhanced_raw = _batched_fetch(discovery, query_uris, ids[0], enhanced=True)
    for name, raw in (("total", total_raw),
                      ("enhanced daily living", enhanced_raw)):
        missing = sorted(queried - set(raw))
        if missing:
            raise ValueError(f"{month} {name}: the API returned no item for "
                             f"{len(missing)} of {len(queried)} areas "
                             f"{missing[:3]}")
    records = build_records(total_raw, enhanced_raw, geo["lad_to_uris"], month)
    check_month_records(records, month)
    return records


# ---------------------------------------------------------------------------
# Compare and store
# ---------------------------------------------------------------------------

_COLS = "lad24cd, pip_total_claimants, pip_enhanced_daily_living"


def _stored(cur, spec, month: str, against: str):
    """({lad24cd: (total, enhanced)}, label) of what the month is compared
    with: its latest stored edition, or (against='live') the live table.
    Empty dict if the month has none. Halts on a broken chain."""
    if against == "live":
        cur.execute(f"SELECT {_COLS} FROM public.{spec.live_table} "
                    "WHERE month = %s", (month,))
        return {a: (b, c) for a, b, c in cur.fetchall()}, "live"
    if against != "editions":
        raise ValueError(f"against must be 'editions' or 'live', not {against!r}")
    try:
        tip = core.chain_tip(cur, spec, month)
    except LookupError:
        return {}, "no edition"
    except ValueError as e:
        halt(f"{spec.editions_table}: {e}")
    cur.execute(f"SELECT {_COLS} FROM public.{spec.editions_table} "
                "WHERE month = %s AND edition = %s", (month, tip))
    return {a: (b, c) for a, b, c in cur.fetchall()}, f"edition {tip}"


LIVE_MISSING = "live-missing"


def live_row_count(cur, spec, month: str) -> int:
    cur.execute(f"SELECT COUNT(*) FROM public.{spec.live_table} "
                "WHERE month = %s", (month,))
    return cur.fetchone()[0]


def live_missing_months(cur, spec) -> list:
    """Months with editions but no live rows (stranded). Ascending."""
    cur.execute(f"SELECT DISTINCT e.month FROM public.{spec.editions_table} e "
                f"WHERE NOT EXISTS (SELECT 1 FROM public.{spec.live_table} l "
                "WHERE l.month = e.month) ORDER BY 1")
    return [r[0] for r in cur.fetchall()]


def insert_live(cur, spec, month: str, records: list) -> None:
    """The month's live rows (loaded_at takes its default, now())."""
    from psycopg2.extras import execute_values
    execute_values(cur, f"INSERT INTO public.{spec.live_table} "
                   "(lad24cd, month, pip_total_claimants, "
                   "pip_enhanced_daily_living) VALUES %s",
                   [(r["lad24cd"], month, r["pip_total_claimants"],
                     r["pip_enhanced_daily_living"]) for r in records],
                   page_size=1000)


def check_live_equals_edition(cur, spec, month: str, edition: int) -> list:
    """Problems unless the month's live rows equal the edition cell for cell
    (EXCEPT ALL both ways, NULL equals NULL, NULL differs from 0) and hold
    EXPECTED_AREAS areas, one row each."""
    bad = []
    n = core.rows_differing(cur, spec, month, edition)
    if n:
        bad.append(f"{month}: {n} live rows differ from edition {edition}")
    cur.execute(f"SELECT COUNT(DISTINCT lad24cd), COUNT(*) "
                f"FROM public.{spec.live_table} WHERE month = %s", (month,))
    got = tuple(cur.fetchone())
    if got != (EXPECTED_AREAS, EXPECTED_AREAS):
        bad.append(f"{month}: live (areas, rows) {got}, expected "
                   f"({EXPECTED_AREAS}, {EXPECTED_AREAS})")
    return bad


def compare_month(cur, spec, month: str, records: list,
                  against: str = "editions") -> dict:
    """{kind, changed, examples, against}: kind 'new' (nothing stored),
    'unchanged' or 'revised'; changed counts areas whose values differ (an
    area on one side only counts; NULL equals NULL, NULL differs from 0); up
    to three examples. Against the editions, an unchanged month with no live
    rows is LIVE_MISSING: load inserts its live rows (no new edition)."""
    old, label = _stored(cur, spec, month, against)
    new = {r["lad24cd"]: (r["pip_total_claimants"],
                          r["pip_enhanced_daily_living"]) for r in records}
    if not old:
        return {"kind": "new", "changed": len(new), "examples": [],
                "against": label}
    diff = [k for k in sorted(set(old) | set(new))
            if k not in old or k not in new or old[k] != new[k]]
    ex = [f"{k} (total, enhanced): {old.get(k, 'absent')} -> "
          f"{new.get(k, 'absent')}" for k in diff[:3]]
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
    then halt unless live equals `edition` and is 296 areas."""
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
    edition cell for cell with 296 areas, before returning; any failure
    halts so the caller's per-month savepoint rolls the whole month back.
    Never commits. A return to the content of an older, non-tip edition is
    a revision against the tip (RULES 2.1) and is stored as a new edition:
    insert_edition(allow_revert=True)."""
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
    If stats is given it is filled for the run log: months, kinds,
    stored_rows (edition rows stored) and live_rows (live rows inserted)."""
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
            cur.execute("SAVEPOINT s19_month")
            in_sp = True
            cmp = compare_month(cur, spec, month, records, against)
            kind = cmp["kind"]
            if write:
                kind = apply_month(cur, spec, month, records,
                                   fetched_on=fetched_on)
            if commit:
                cur.execute("RELEASE SAVEPOINT s19_month")
                in_sp = False
                conn.commit()
            else:
                cur.execute("ROLLBACK TO SAVEPOINT s19_month")
                cur.execute("RELEASE SAVEPOINT s19_month")
                in_sp = False
        except (Exception, SystemExit) as e:
            if in_sp:
                cur.execute("ROLLBACK TO SAVEPOINT s19_month")
                cur.execute("RELEASE SAVEPOINT s19_month")
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
        print(f"  {month}: {kind}, {cmp['changed']} areas "
              f"{'new' if kind == 'new' else 'changed'} (compared with "
              f"{cmp['against']})"
              + ("; " + "; ".join(cmp["examples"]) if cmp["examples"] else "")
              + action + (" COMMITTED" if commit else ""))
    _print_tally(tally)
    return 0


def _print_tally(tally):
    print("  summary: " + ", ".join(
        f"{k} {n} month(s)/{c} areas" for k, (n, c) in tally.items()))


RUN_AGENT = "Source 19 - PIP Claimants"
RUN_SOURCE = "19"


def log_run(cur, rows_written: int, notes: str, started_at=None) -> None:
    """Write the pipeline_run_log row for a committed run (status 'success'
    is the only value the table accepts for new rows). source_number and
    source_code are both '19'. Called only on `sync-new --commit` and
    `load --commit`; previews and --simulate never reach it."""
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
            "Measures: pip_total_claimants, pip_enhanced_daily_living. Rows "
            f"stored: {stats['stored_rows']} edition rows, {live_rows} live "
            "rows inserted. A run that finds nothing new is logged because "
            "the check is the run.")


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
    """editions_core.status with SPEC under the key names the S1b, RO4 and
    S8b wrappers use ('drift' is 'drift_periods', 'forked' is
    'chain_errors'), plus 'live_missing' (months with editions but no live
    rows) and 'months_migrated' (months_migrated); either makes ok false."""
    st = core.status(cur, spec)
    missing = live_missing_months(cur, spec)
    migrated = months_migrated(cur, spec)
    return {"new_periods": st["new_periods"], "drift_periods": st["drift"],
            "pending_refresh": st["pending_refresh"],
            "chain_errors": st["forked"], "bad_counts": st["bad_counts"],
            "live_missing": missing, "months_migrated": migrated,
            "periods": st["periods"],
            "ok": st["ok"] and not missing and migrated}


def format_status(st: dict) -> str:
    from s1_editions import format_status as _format_status
    text = _format_status(st, "S19 PIP claimants").replace(
        "periods in the live table", "months in the live table")
    lines = []
    if not st.get("months_migrated", True):
        lines.append(f"  MONTH LABELS NOT MIGRATED: {_unmigrated()}")
    lines += [f"  editions month missing from live: {mo} (run load --commit "
              "to insert its live rows)" for mo in st.get("live_missing") or []]
    if not lines:
        return text
    head, tail = text.rsplit("\n", 1)
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
                if not months_migrated(cur):
                    print(f"status: {_unmigrated()}")
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
            if not months_migrated(cur):
                halt(_unmigrated())
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
            if not months_migrated(cur):
                halt(_unmigrated())
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


def format_migration(rep: dict) -> str:
    """The relabel plan and its checks, from migrate_months' report."""
    b, a = rep["before"], rep["after"]
    lines = [f"migrate-months: {rep['table']}: {b['rows']} rows in "
             f"{len(b['months'])} months", "plan (label -> key):"]
    for label, key in sorted(rep["mapping"].items(), key=lambda kv: kv[1]):
        f = b["per_month"].get(label, {})
        lines.append(f"  {label} -> {key}: {f.get('rows')} rows, total sum "
                     f"{f.get('sum_total')}, enhanced sum "
                     f"{f.get('sum_enhanced')}, NULLs "
                     f"{f.get('null_total')}/{f.get('null_enhanced')}")
    probs = _compare_states(b, a, rep["mapping"])
    lines += ["checks (before vs after the relabel):",
              f"  {'PASS' if b['rows'] == a['rows'] else 'FAIL'} row count "
              f"{b['rows']} -> {a['rows']}",
              f"  {'PASS' if len(b['months']) == len(a['months']) else 'FAIL'}"
              f" distinct months {len(b['months'])} -> {len(a['months'])}",
              f"  {'PASS' if not probs else 'FAIL'} per-month rows, totals "
              "and NULL counts identical under the plan",
              f"  {'PASS' if b['latest'] == a['latest'] else 'FAIL'} "
              f"MAX(month) selects {rep['latest_key']} (was "
              f"{rep['latest_label']}); its per-area values identical"]
    if probs:
        lines.append("  problems: " + "; ".join(probs))
    lines.append("reverse mapping (key -> label): " + ", ".join(
        f"{k} -> {lb}" for k, lb in sorted(rep["reverse"].items())))
    return "\n".join(lines)


def cmd_migrate_months(args) -> int:
    """The one-off relabel. Always on a writable connection: the preview
    runs the relabel and its checks (migrate_months) and rolls back, which a
    read-only session cannot do; the lock (SHARE ROW EXCLUSIVE: blocks
    writers, not readers) is taken first and released with the
    transaction. Only --commit commits, once."""
    conn = _conn(True)
    try:
        with conn.cursor() as cur:
            cur.execute(f"LOCK TABLE public.{LIVE} IN SHARE ROW EXCLUSIVE MODE")
            rep = migrate_months(cur, LIVE, commit=args.commit)
        if rep["status"] == "already migrated":
            conn.rollback()
            print(f"migrate-months: every month in {LIVE} is already a "
                  "yyyymm key; nothing to do")
            return 0
        print(format_migration(rep))
        if args.commit:
            conn.commit()
            print("migrate-months: relabel COMMITTED")
        else:
            conn.rollback()
            print(("SIMULATION" if args.simulate else "PREVIEW")
                  + ": relabel run and checked, then ROLLED BACK (nothing "
                  "persisted; use --commit to keep it)")
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
            # first, before any API call: never guess a month's form
            if not months_migrated(cur):
                halt(_unmigrated())
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
            discovery = get_discovery()
            available = get_available_months(discovery)
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
            geo = resolve_geography(discovery, conn)
            stats = {}
            started = datetime.now(timezone.utc)
            rc = load_months(cur, SPEC, months,
                             lambda mo: fetch_month(mo, discovery, geo),
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
    ap = argparse.ArgumentParser(description="S19 PIP claimants: editions "
                                 "loader")
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
    p = sub.add_parser("migrate-months", help="one-off relabel of the live "
                       "table's months to yyyymm keys (preview by default)")
    _mode(p)
    p.set_defaults(func=cmd_migrate_months)
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
