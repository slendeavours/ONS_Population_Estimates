"""Editions core: the append-only editions machinery, once, driven by a spec.

A source that revises keeps two tables: the live table (one row per key per
period, the latest layer) and its editions table (every published edition of
every period, never overwritten). This module owns the editions table for any
such source, described by an EditionSpec.

Write side (this file so far):
    EditionSpec      what a source's tables look like
    halt             stop with a clear message (SystemExit)
    create_schema    editions table + append-only triggers; idempotent
    insert_edition   one new edition of a period; supersedes must be the tip
    latest_edition   tip of a period's supersedes chain, whole chain validated

Extracted from s1_editions.py (create_schema, insert_edition and the chain
validator in latest_edition), s1b_editions.py and ro4_editions.py.

Metadata columns owned by the core on every editions table: edition (part of
the primary key), release_label, published_date, source_file, source_sha256,
supersedes, loaded_at. Anything else (source_url, S1b's category columns,
RO4's source and data_missing) is declared in value_cols or extra_cols and
supplied in each row dict.

Trigger names follow the existing pattern <name>_editions_immutable (row
trigger on UPDATE or DELETE, and the function both triggers call) and
<name>_editions_no_truncate (statement trigger on TRUNCATE), so for the real
specs (name 's1', 's1b', 'ro4') create_schema reproduces the existing triggers
and functions exactly. The function body, and so the error message, is the
same text the three modules use.
"""
import re
import sys
from dataclasses import dataclass
from typing import Callable, NoReturn

_IDENT = re.compile(r"[a-z_][a-z0-9_]*")

# (column, SQL) of the metadata every editions table carries, after the data
META_COLS = (
    ("release_label", "text"),
    ("published_date", "date"),
    ("source_file", "text"),
    ("source_sha256", "text"),
    ("supersedes", "integer"),
    ("loaded_at", "timestamptz NOT NULL DEFAULT now()"),
)


def _ident(name: str) -> str:
    """A bare lower-case SQL identifier, or ValueError (names are
    interpolated into SQL, so anything else is refused)."""
    if not isinstance(name, str) or not _IDENT.fullmatch(name):
        raise ValueError(f"not a plain SQL identifier: {name!r}")
    return name


@dataclass(frozen=True, kw_only=True)
class EditionSpec:
    """One revising source's live and editions tables.

    key_cols: the columns that identify a row within a period (e.g.
    ('lad24cd',), or ('lad24cd', 'category_code') for S1b).
    period_col: the period column ('period', or 'financial_year' for RO4).
    value_cols / extra_cols: (column, SQL type and constraints) of the data
    columns; value_cols are the measures, extra_cols any other per-row
    columns (source_url and the like). Both are copied from each row dict;
    a missing key is stored NULL.
    refresh_cols: the live columns refresh-latest may overwrite (Task 3).
    expected_rows_per_period: optional callable giving a period's row count.
    fk_la_boundaries: add the lad24cd -> la_boundaries foreign key when
    'lad24cd' is a key column (the real tables have it; throwaway test
    tables pass False).
    key_types: optional (column, SQL) for key and period columns; a column
    not listed is 'text NOT NULL' (lad24cd: 'varchar(9) NOT NULL').
    table_constraints: optional extra table-level constraint clauses.
    """
    name: str
    live_table: str
    editions_table: str
    key_cols: tuple
    period_col: str
    value_cols: tuple
    extra_cols: tuple = ()
    refresh_cols: tuple
    expected_rows_per_period: "Callable | None" = None
    fk_la_boundaries: bool = True
    key_types: tuple = ()
    table_constraints: tuple = ()

    def __post_init__(self):
        for n in (self.name, self.live_table, self.editions_table,
                  self.period_col, *self.key_cols, *self.refresh_cols,
                  *(c for c, _ in self.value_cols),
                  *(c for c, _ in self.extra_cols),
                  *(c for c, _ in self.key_types)):
            _ident(n)
        cols = self.data_cols + ("edition",) + tuple(c for c, _ in META_COLS)
        if len(set(cols)) != len(cols):
            raise ValueError(f"{self.name}: a column is declared twice {cols}")

    @property
    def data_cols(self) -> tuple:
        """Key, period, value and extra columns, in table order."""
        return (tuple(self.key_cols) + (self.period_col,)
                + tuple(c for c, _ in self.value_cols)
                + tuple(c for c, _ in self.extra_cols))

    @property
    def trigger(self) -> str:
        return f"{self.name}_editions_immutable"

    @property
    def truncate_trigger(self) -> str:
        return f"{self.name}_editions_no_truncate"


def halt(msg: str) -> NoReturn:
    sys.exit(f"HALT: {msg}")


def create_schema(cur, spec: EditionSpec) -> None:
    """Create the editions table and its append-only triggers if absent.

    Idempotent: CREATE TABLE IF NOT EXISTS leaves an existing table as it is,
    and the function and triggers are CREATE OR REPLACE with the same names
    and body. Primary key: key_cols + (period_col, 'edition')."""
    t = spec.editions_table
    types = dict(spec.key_types)
    lines = []
    for c in tuple(spec.key_cols) + (spec.period_col,):
        sql = types.get(c, "varchar(9) NOT NULL" if c == "lad24cd"
                        else "text NOT NULL")
        if c == "lad24cd" and spec.fk_la_boundaries:
            sql += " REFERENCES public.la_boundaries (lad24cd)"
        lines.append(f"{c} {sql}")
    lines.append("edition integer NOT NULL")
    lines += [f"{c} {sql}" for c, sql in spec.value_cols]
    lines += [f"{c} {sql}" for c, sql in spec.extra_cols]
    lines += [f"{c} {sql}" for c, sql in META_COLS]
    lines += list(spec.table_constraints)
    pk = ", ".join(tuple(spec.key_cols) + (spec.period_col, "edition"))
    lines.append(f"PRIMARY KEY ({pk})")
    body = ",\n        ".join(lines)
    cur.execute(f"CREATE TABLE IF NOT EXISTS public.{t} (\n        {body}\n    )")
    cur.execute(f"""
    CREATE OR REPLACE FUNCTION public.{spec.trigger}() RETURNS trigger AS $f$
    BEGIN
        RAISE EXCEPTION '{t} is append-only: % is not permitted', TG_OP;
    END
    $f$ LANGUAGE plpgsql""")
    cur.execute(f"""CREATE OR REPLACE TRIGGER {spec.trigger}
        BEFORE UPDATE OR DELETE ON public.{t}
        FOR EACH ROW EXECUTE FUNCTION public.{spec.trigger}()""")
    cur.execute(f"""CREATE OR REPLACE TRIGGER {spec.truncate_trigger}
        BEFORE TRUNCATE ON public.{t}
        FOR EACH STATEMENT EXECUTE FUNCTION public.{spec.trigger}()""")


def _chain_tip(pairs, period: str) -> int:
    """Tip of one period's supersedes chain from its (edition, supersedes)
    pairs. ValueError unless it is one linear chain: exactly one root
    (supersedes NULL), every other edition supersedes a different existing
    edition, no edition superseded twice (fork), each edition has one
    supersedes value, and walking from the root reaches every edition (so a
    cycle is refused). LookupError if there are no editions. Ordering is the
    chain, never published_date."""
    pairs = sorted(set(pairs), key=lambda p: (p[0], p[1] is None, p[1] or 0))
    if not pairs:
        raise LookupError(f"no editions recorded for {period}")
    sup = {}
    for ed, s in pairs:
        if ed in sup:
            raise ValueError(f"{period}: edition {ed} has inconsistent "
                             "supersedes values across its rows")
        sup[ed] = s
    roots = sorted(e for e, s in sup.items() if s is None)
    if len(roots) != 1:
        raise ValueError(f"{period}: {len(roots)} editions {roots} have no "
                         "supersedes; the chain has no single root")
    for e, s in sorted(sup.items()):
        if s is not None and (s == e or s not in sup):
            raise ValueError(f"{period}: edition {e} supersedes "
                             f"{'itself' if s == e else f'missing edition {s}'}")
    children = {}
    for e, s in sup.items():
        if s is not None:
            children.setdefault(s, []).append(e)
    if any(len(c) > 1 for c in children.values()):
        tips = sorted(e for e in sup if e not in children)
        raise ValueError(f"{period}: supersedes chain has {len(tips)} tips "
                         f"{tips}; order is ambiguous (fork)")
    tip, seen = roots[0], {roots[0]}
    while tip in children:
        tip = children[tip][0]
        if tip in seen:
            break
        seen.add(tip)
    if seen != set(sup):
        raise ValueError(f"{period}: the chain from root {roots[0]} reaches "
                         f"{sorted(seen)} but editions {sorted(sup)} exist "
                         "(cycle or disconnected editions)")
    return tip


def chain_tip(cur, spec: EditionSpec, period: str) -> int:
    """As latest_edition, but raises LookupError (no editions) or ValueError
    (broken chain) instead of halting, for callers that report rather than
    stop (status, Task 3)."""
    cur.execute(f"SELECT DISTINCT edition, supersedes "
                f"FROM public.{spec.editions_table} "
                f"WHERE {spec.period_col} = %s", (period,))
    return _chain_tip(cur.fetchall(), period)


def latest_edition(cur, spec: EditionSpec, period: str) -> int:
    """Tip of the period's supersedes chain; halt if the period has no
    editions or the chain is not one linear chain (see _chain_tip)."""
    try:
        return chain_tip(cur, spec, period)
    except (LookupError, ValueError) as e:
        halt(f"{spec.editions_table}: {e}")


def insert_edition(cur, spec: EditionSpec, rows: list, period: str, *,
                   release_label: str, published_date: "date | None",
                   source_file: "str | None", source_sha256: str,
                   supersedes: "int | None") -> int:
    """Insert one edition of a period; return its edition number.

    If source_sha256 is already recorded for the period nothing is inserted
    and that edition's number is returned (a re-run is harmless). Otherwise:
    rows must be non-empty, every key column present in each row, any period
    in a row equal to `period`, and `supersedes` must be the period's current
    chain tip (None only when the period has no editions); anything else
    halts. A missing value or extra column is stored NULL, never 0."""
    from psycopg2.extras import execute_values
    t, pc = spec.editions_table, spec.period_col
    if not rows:
        halt(f"insert_edition: no rows supplied for {period}")
    cur.execute(f"SELECT DISTINCT edition FROM public.{t} "
                f"WHERE {pc} = %s AND source_sha256 = %s",
                (period, source_sha256))
    existing = [r[0] for r in cur.fetchall()]
    if existing:
        return existing[0]
    try:
        tip = chain_tip(cur, spec, period)
    except LookupError:
        tip = None
    except ValueError as e:
        halt(f"insert_edition: {t}: {e}")
    if supersedes != tip:
        halt(f"insert_edition: supersedes={supersedes} is not the current "
             f"chain tip of {period} ({tip if tip is not None else 'none, the period has no editions'})")
    cur.execute(f"SELECT COALESCE(MAX(edition), 0) FROM public.{t} "
                f"WHERE {pc} = %s", (period,))
    edition = cur.fetchone()[0] + 1
    for r in rows:
        if r.get(pc, period) != period:
            halt(f"insert_edition: rows contain a {pc} other than {period}")
        missing = [c for c in spec.key_cols if c not in r]
        if missing:
            halt(f"insert_edition: a row of {period} lacks key column(s) "
                 f"{missing}")
    other = spec.data_cols[len(spec.key_cols) + 1:]
    cols = spec.data_cols + ("edition", "release_label", "published_date",
                             "source_file", "source_sha256", "supersedes")
    data = [[r[c] for c in spec.key_cols] + [period]
            + [r.get(c) for c in other]
            + [edition, release_label, published_date, source_file,
               source_sha256, supersedes]
            for r in rows]
    execute_values(cur, f"INSERT INTO public.{t} ({', '.join(cols)}) "
                   "VALUES %s", data, page_size=1000)
    return edition
