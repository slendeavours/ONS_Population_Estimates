"""Compare two W1 runs cell for cell.

For each W1 output table the two runs are joined on the table's key columns
(its primary key minus run_id) and every column not in the ignore set is
compared with IS DISTINCT FROM, so NULL equals NULL and NULL differs from 0.
As a second, stricter test the values' text forms are compared too, which
catches a change of numeric scale (1.5 against 1.50) that = treats as equal.
Row-count differences and keys present in one run only are reported as well.

A table with no primary key is compared as a multiset of whole rows (all
non-ignored columns, EXCEPT ALL both ways), and the differences say so.

The four W1 output tables have no timestamp column (checked on 8 Oct 2026),
so in practice only run_id is ignored. The default ignore set also names
created_at, loaded_at and run_date so a timestamp column added later does not
make two identical runs look different; any other column is compared.

Usage:
    python scripts/w1_compare_runs.py A B
Prints a short table, lists any differences, and exits 1 if there are any.
The CLI connects read-only.
"""
import sys
from pathlib import Path

from psycopg2 import sql

sys.path.insert(0, str(Path(__file__).resolve().parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

W1_TABLES = ("staging_la_signals", "staging_national",
             "staging_tenant_type_rankings", "staging_convergence")
DEFAULT_IGNORE = frozenset({"run_id", "created_at", "loaded_at", "run_date"})


def _columns(cur, table):
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position""", (table,))
    cols = [r[0] for r in cur.fetchall()]
    if not cols:
        raise ValueError(f"table {table} not found")
    return cols


def _primary_key(cur, table):
    cur.execute("""
        SELECT a.attname
        FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid
                           AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = %s::regclass AND i.indisprimary
        ORDER BY array_position(i.indkey, a.attnum)""", (f"public.{table}",))
    return [r[0] for r in cur.fetchall()]


def _count(cur, table, run):
    cur.execute(sql.SQL("SELECT count(*) FROM {} WHERE run_id = %s")
                .format(sql.Identifier(table)), (run,))
    return cur.fetchone()[0]


def _differs(col):
    a, b = sql.Identifier("a", col), sql.Identifier("b", col)
    return sql.SQL("({a} IS DISTINCT FROM {b} OR "
                   "{a}::text IS DISTINCT FROM {b}::text)").format(a=a, b=b)


def _compare_keyed(cur, table, run_a, run_b, keys, cols):
    diffs = []
    t = sql.Identifier(table)
    key_list = sql.SQL(", ").join(
        sql.SQL("COALESCE(a.{k}, b.{k})::text").format(k=sql.Identifier(k))
        for k in keys) if keys else sql.SQL("NULL::text")
    on = (sql.SQL(" AND ").join(
        sql.SQL("a.{k} = b.{k}").format(k=sql.Identifier(k)) for k in keys)
        if keys else sql.SQL("TRUE"))
    a_key = sql.Identifier("a", (keys or ["run_id"])[0])
    b_key = sql.Identifier("b", (keys or ["run_id"])[0])
    cells = sql.SQL(", ").join(
        sql.SQL("CASE WHEN {d} THEN format('%%s: %%s -> %%s', {name}, "
                "COALESCE({a}::text, 'NULL'), COALESCE({b}::text, 'NULL')) "
                "END").format(d=_differs(c), name=sql.Literal(c),
                              a=sql.Identifier("a", c),
                              b=sql.Identifier("b", c))
        for c in cols) if cols else sql.SQL("NULL::text")
    query = sql.SQL("""
        SELECT ARRAY[{key_list}], {a_key} IS NOT NULL, {b_key} IS NOT NULL,
               array_remove(ARRAY[{cells}]::text[], NULL)
        FROM (SELECT * FROM {t} WHERE run_id = %(a)s) a
        FULL OUTER JOIN (SELECT * FROM {t} WHERE run_id = %(b)s) b ON {on}
        ORDER BY 1""").format(key_list=key_list, a_key=a_key, b_key=b_key,
                              cells=cells, t=t, on=on)
    # Keyed by run_id alone (staging_national): ON TRUE pairs the single row
    # of each run, and a.run_id / b.run_id show which side is present.
    cur.execute(query, {"a": run_a, "b": run_b})
    for key_vals, in_a, in_b, changed in cur.fetchall():
        key = ", ".join(f"{k}={v}" for k, v in zip(keys, key_vals)) or "(row)"
        if in_a and not in_b:
            diffs.append(f"{key}: only in run {run_a}")
        elif in_b and not in_a:
            diffs.append(f"{key}: only in run {run_b}")
        else:
            diffs.extend(f"{key}: {c}" for c in changed)
    return diffs


def _compare_multiset(cur, table, run_a, run_b, cols):
    t = sql.Identifier(table)
    col_list = sql.SQL(", ").join(sql.Identifier(c) for c in cols)
    diffs = []
    for first, second in ((run_a, run_b), (run_b, run_a)):
        cur.execute(sql.SQL("""
            SELECT row_to_json(x)::text FROM (
                SELECT {c} FROM {t} WHERE run_id = %(x)s
                EXCEPT ALL
                SELECT {c} FROM {t} WHERE run_id = %(y)s) x
            ORDER BY 1""").format(c=col_list, t=t), {"x": first, "y": second})
        diffs.extend(f"no primary key, compared as a multiset: row {r[0]} "
                     f"only in run {first}" for r in cur.fetchall())
    return diffs


def compare_runs(cur, run_a: int, run_b: int, *,
                 ignore: frozenset[str] = DEFAULT_IGNORE,
                 tables=W1_TABLES) -> dict[str, list[str]]:
    """Table name -> list of differences between run_a and run_b.

    Empty lists mean identical. NULL equals NULL; NULL differs from 0.
    """
    result = {}
    for table in tables:
        cols = _columns(cur, table)
        pk = _primary_key(cur, table)
        compared = [c for c in cols if c not in ignore]
        diffs = []
        n_a, n_b = _count(cur, table, run_a), _count(cur, table, run_b)
        if n_a != n_b:
            diffs.append(f"row count: run {run_a} has {n_a}, "
                         f"run {run_b} has {n_b}")
        if pk:
            keys = [k for k in pk if k != "run_id"]
            values = [c for c in compared if c not in keys]
            diffs.extend(_compare_keyed(cur, table, run_a, run_b, keys,
                                        values))
        else:
            diffs.extend(_compare_multiset(cur, table, run_a, run_b,
                                           compared))
        result[table] = diffs
    return result


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print(__doc__)
        return 2
    run_a, run_b = int(args[0]), int(args[1])
    from _db import get_readonly_conn
    conn = get_readonly_conn()
    try:
        with conn.cursor() as cur:
            counts = {t: (_count(cur, t, run_a), _count(cur, t, run_b))
                      for t in W1_TABLES}
            result = compare_runs(cur, run_a, run_b)
    finally:
        conn.close()
    print(f"W1 run {run_a} vs run {run_b} (ignored: "
          f"{', '.join(sorted(DEFAULT_IGNORE))})")
    print(f"{'table':32} {'rows ' + str(run_a):>10} {'rows ' + str(run_b):>10}"
          f" {'differences':>12}")
    for t in W1_TABLES:
        print(f"{t:32} {counts[t][0]:>10} {counts[t][1]:>10} "
              f"{len(result[t]):>12}")
    total = sum(len(d) for d in result.values())
    for t, diffs in result.items():
        for d in diffs:
            print(f"  {t}: {d}")
    print("IDENTICAL" if total == 0 else f"DIFFERENT: {total} difference(s)")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
