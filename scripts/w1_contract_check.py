"""W1 node 5 <-> staging_la_signals column contract check.

The defect this exists to prevent: the S9 and S19 integrations were applied
to the database by direct SQL and never written back to the stored n8n node,
so the node was two builds behind the table for a month. The next genuine
workflow run would have produced six null columns and nobody would have been
told.

The check runs in three directions:

  ERROR  a table column the node never writes         -> the S9/S19 failure
  ERROR  a node column that is not in the table       -> would throw anyway,
                                                         but fail early
  ERROR  a positional mismatch between the INSERT
         column list and the SELECT list              -> a node naming a
                                                         column that exists
                                                         but populating it
                                                         from the wrong
                                                         expression. This is
                                                         the one that would
                                                         not throw.
  WARN   an inserted column with no matching
         `col = EXCLUDED.col` on conflict             -> silently stale on a
                                                         same-run re-run

It also refreshes `staging_signal_contract`, which is what the in-workflow
pre-flight node compares against. That node runs inside W1 itself, because
W1 has been run without exporting and the export-time check alone would let
a divergence sit undetected until the next publish.

Read-only against the workflow. Writes only the contract table.
"""
import datetime
import hashlib
import re
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _db import ENV, get_conn  # noqa: E402

W1_ID = "IrrglXLYcphSg5bC"
NODE5_NAME = "LA Signals"
TABLE = "staging_la_signals"
KEY_COLUMNS = {"run_id", "lad24cd"}

CONTRACT_DDL = """
CREATE TABLE IF NOT EXISTS staging_signal_contract (
    column_name  VARCHAR(63) NOT NULL PRIMARY KEY,
    ordinal      INTEGER     NOT NULL,
    source_expr  TEXT,
    refreshed_on_conflict BOOLEAN NOT NULL,
    node_query_sha256 TEXT,
    recorded_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE staging_signal_contract
    ADD COLUMN IF NOT EXISTS node_query_sha256 TEXT;

COMMENT ON TABLE staging_signal_contract IS
 'The columns W1 node 5 writes, as parsed from the stored workflow. '
 'Refreshed by scripts/w1_contract_check.py whenever node 5 changes. The '
 'W1 pre-flight node compares staging_la_signals against this and aborts '
 'the run on divergence.';
"""


def n8n_conn():
    return psycopg2.connect(host="localhost", port=5432, dbname="n8ndb",
                            user=ENV["PG_USER"], password=ENV["PG_PASSWORD"])


# ── SQL parsing ─────────────────────────────────────────────────────────────

def strip_comments(sql):
    out, i, n = [], 0, len(sql)
    in_str = False
    while i < n:
        ch = sql[i]
        if in_str:
            out.append(ch)
            if ch == "'":
                in_str = False
            i += 1
            continue
        if ch == "'":
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            while i < n and sql[i] != "\n":
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def split_top_level(text):
    """Split on commas at parenthesis depth zero, outside string literals."""
    items, buf, depth, in_str = [], [], 0, False
    for ch in text:
        if in_str:
            buf.append(ch)
            if ch == "'":
                in_str = False
            continue
        if ch == "'":
            in_str = True
            buf.append(ch)
        elif ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            items.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if "".join(buf).strip():
        items.append("".join(buf).strip())
    return items


def parse_node5(sql):
    clean = strip_comments(sql)

    m = re.search(r"INSERT\s+INTO\s+" + TABLE + r"\s*\((.*?)\)\s*SELECT",
                  clean, re.S | re.I)
    if not m:
        raise ValueError("could not find the INSERT column list in node 5")
    insert_cols = [c.strip() for c in split_top_level(m.group(1))]

    sel = re.search(r"\)\s*SELECT\s+(.*?)\bFROM\s+la_boundaries\b",
                    clean, re.S | re.I)
    if not sel:
        raise ValueError("could not find the SELECT list in node 5")
    select_items = split_top_level(sel.group(1))

    setm = re.search(r"ON\s+CONFLICT\s*\([^)]*\)\s*DO\s+UPDATE\s+SET\s+(.*)$",
                     clean, re.S | re.I)
    set_cols = set()
    if setm:
        for item in split_top_level(setm.group(1).rstrip().rstrip(";")):
            mm = re.match(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*=", item)
            if mm:
                set_cols.add(mm.group(1).lower())

    return insert_cols, select_items, set_cols


def alias_of(select_item):
    """The column name a SELECT item lands in, or None if undeterminable."""
    s = " ".join(select_item.split())
    m = re.search(r"\bAS\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*$", s, re.I)
    if m:
        return m.group(1).lower()
    if re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)?", s):
        return s.split(".")[-1].lower()
    return None


# ── The check ───────────────────────────────────────────────────────────────

def check(refresh_contract=True):
    """Returns (errors, warnings, contract_rows)."""
    nc = n8n_conn()
    ncur = nc.cursor()
    ncur.execute("SELECT nodes FROM workflow_entity WHERE id = %s", (W1_ID,))
    row = ncur.fetchone()
    if not row:
        raise ValueError(f"workflow {W1_ID} not found in n8ndb")
    node = next((n for n in row[0] if n["name"] == NODE5_NAME), None)
    nc.close()
    if node is None:
        raise ValueError(f"node '{NODE5_NAME}' not found in Workflow 1")

    node_sql = node["parameters"]["query"]
    node_sha = hashlib.sha256(node_sql.encode("utf-8")).hexdigest()
    insert_cols, select_items, set_cols = parse_node5(node_sql)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT column_name FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = %s
         ORDER BY ordinal_position
    """, (TABLE,))
    table_cols = [r[0] for r in cur.fetchall()]

    errors, warnings = [], []

    missing_from_node = [c for c in table_cols if c not in insert_cols]
    for c in missing_from_node:
        errors.append(
            f"`{TABLE}.{c}` exists in the table but W1 node 5 never writes "
            "it. A workflow run would leave it null. This is the S9/S19 "
            "failure mode: applied by direct SQL, never written back.")

    missing_from_table = [c for c in insert_cols if c not in table_cols]
    for c in missing_from_table:
        errors.append(
            f"W1 node 5 writes `{c}`, which does not exist on `{TABLE}`.")

    if len(insert_cols) != len(select_items):
        errors.append(
            f"node 5 INSERT names {len(insert_cols)} columns but SELECT "
            f"returns {len(select_items)} expressions. Positional alignment "
            "cannot be checked and the statement is almost certainly wrong.")
    else:
        for i, (col, item) in enumerate(zip(insert_cols, select_items)):
            alias = alias_of(item)
            if alias is None:
                warnings.append(
                    f"position {i} (`{col}`): the SELECT expression has no "
                    "alias and no plain column reference, so it cannot be "
                    f"checked against the column name. Expression: "
                    f"{' '.join(item.split())[:80]}")
            elif alias != col.lower():
                errors.append(
                    f"position {i}: node 5 inserts into `{col}` but the "
                    f"SELECT expression at that position resolves to "
                    f"`{alias}`. The column exists, so this would not throw "
                    "— it would silently populate the column from the wrong "
                    "expression.")

    for c in insert_cols:
        if c.lower() in KEY_COLUMNS:
            continue
        if c.lower() not in set_cols:
            warnings.append(
                f"`{c}` is inserted but has no `{c} = EXCLUDED.{c}` on "
                "conflict, so re-running the same run_id leaves the old "
                "value in place.")

    contract = []
    if len(insert_cols) == len(select_items):
        for i, (col, item) in enumerate(zip(insert_cols, select_items)):
            contract.append((col, i, " ".join(item.split())[:500],
                             col.lower() in set_cols
                             or col.lower() in KEY_COLUMNS, node_sha))

    if refresh_contract and contract and not errors:
        cur.execute(CONTRACT_DDL)
        cur.execute("DELETE FROM staging_signal_contract")
        cur.executemany(
            "INSERT INTO staging_signal_contract "
            "(column_name, ordinal, source_expr, refreshed_on_conflict, "
            "node_query_sha256) VALUES (%s, %s, %s, %s, %s)", contract)
        conn.commit()

    conn.close()
    return errors, warnings, contract


def main():
    try:
        errors, warnings, contract = check()
    except ValueError as e:
        sys.exit(f"HALT: W1 contract check could not run — {e}")

    print(f"W1 node 5 contract check — {len(contract)} columns")
    for w in warnings:
        print(f"  WARN  {w}")
    for e in errors:
        print(f"  ERROR {e}")
    if errors:
        sys.exit(f"\nHALT: {len(errors)} divergence(s) between W1 node 5 and "
                 f"{TABLE}. Fix the node before running the workflow — a run "
                 "in this state writes columns the node does not know about "
                 "as null.")
    print(f"  OK    node 5 and {TABLE} agree in both directions, "
          f"{len(warnings)} warning(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
