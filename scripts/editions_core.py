"""Editions core: the append-only editions machinery, once, driven by a spec.

A source that revises keeps two tables: the live table (one row per key per
period, the latest layer) and its editions table (every published edition of
every period, never overwritten). This module owns the editions table for any
such source, described by an EditionSpec.

Write side:
    EditionSpec      what a source's tables look like
    halt             stop with a clear message (SystemExit)
    create_schema    editions table + append-only triggers; idempotent
    insert_edition   one new edition of a period; supersedes must be the tip
    latest_edition   tip of a period's supersedes chain, whole chain validated

Read side and refresh:
    latest_map       chain tip of every live period; new and broken periods
    status           what needs action (new, pending, drift, counts, forks)
    sync_new         edition 1 'as loaded' for live periods with no editions
    refresh_latest   copy the latest edition into live, hash-guarded
    period_hashes    per-period content hash of the live or editions table

Extracted from s1_editions.py (create_schema, insert_edition, the chain
validator in latest_edition, and the generic read/refresh pieces), compared
with s1b_editions.py and ro4_editions.py; where they differ the stricter
behaviour is taken and noted in the function's docstring.

Transactions: nothing here commits or rolls back the caller's transaction.
sync_new and refresh_latest write inside a savepoint of their own and, if
they halt (or raise) after writing, roll back to that savepoint first, so the
caller's transaction is left exactly as it was given and still usable; on
success the savepoint is released and the caller decides commit or not.
Both therefore refuse to run on an autocommit connection (SAVEPOINT needs a
transaction block).

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
import hashlib
import re
import sys
from collections import Counter
from contextlib import contextmanager
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
    refresh_cols: the live columns refresh-latest may overwrite; nothing
    else in the live table is ever written. Each is copied from the
    same-named editions column (a value, extra or metadata column such as
    source_file) unless refresh_from names another. Those that are value or
    extra columns (compare_cols) decide whether live equals an edition.
    refresh_from: optional (live column, editions column) pairs for refresh
    columns copied from a differently named editions column, e.g. S1's
    ('extracted_at', 'loaded_at'); written, never compared.
    expected_rows_per_period: optional callable (cur, period) -> int | None
    giving a period's live row count for status; default (or None) is the
    most common per-period live count.
    as_loaded_source_col: optional live column whose single value per period
    becomes source_file of a sync-new 'as loaded' edition (S1 'source_file',
    S1b 'source_edition'); None stores source_file NULL.
    fk_la_boundaries: add the lad24cd -> la_boundaries foreign key when
    'lad24cd' is a key column (the real tables have it; throwaway test
    tables pass False).
    key_types: optional (column, SQL) for key and period columns; a column
    not listed is 'text NOT NULL' (lad24cd: 'varchar(9) NOT NULL').
    table_constraints: optional extra table-level constraint clauses.
    as_loaded_date: 'single' (default) = sync-new needs one loaded_at date
    per live period and halts otherwise; 'latest' = a period's live rows may
    carry several load dates and edition 1 takes the latest as its
    published_date (S15 only).
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
    refresh_from: tuple = ()
    as_loaded_source_col: "str | None" = None
    as_loaded_date: str = "single"
    refresh_source_whole_period: bool = False

    def __post_init__(self):
        for n in (self.name, self.live_table, self.editions_table,
                  self.period_col, *self.key_cols, *self.refresh_cols,
                  *(c for c, _ in self.value_cols),
                  *(c for c, _ in self.extra_cols),
                  *(c for c, _ in self.key_types),
                  *(c for pair in self.refresh_from for c in pair),
                  *((self.as_loaded_source_col,)
                    if self.as_loaded_source_col is not None else ())):
            _ident(n)
        if self.as_loaded_date not in ("single", "latest"):
            raise ValueError(f"{self.name}: as_loaded_date must be 'single' "
                             f"or 'latest', not {self.as_loaded_date!r}")
        cols = self.data_cols + ("edition",) + tuple(c for c, _ in META_COLS)
        if len(set(cols)) != len(cols):
            raise ValueError(f"{self.name}: a column is declared twice {cols}")
        keys = tuple(self.key_cols) + (self.period_col,)
        stray = [c for c, _ in self.key_types if c not in keys]
        if stray:
            raise ValueError(f"{self.name}: key_types names non-key column(s) "
                             f"{stray}")
        mapped = dict(self.refresh_from)
        if len(mapped) != len(self.refresh_from):
            raise ValueError(f"{self.name}: refresh_from names a column twice")
        for c in mapped:
            if c not in self.refresh_cols:
                raise ValueError(f"{self.name}: refresh_from column {c} is not "
                                 "a refresh column")
        for live_col, ed_col in self.refresh_from:
            if ed_col not in cols:
                raise ValueError(f"{self.name}: refresh_from {live_col} <- "
                                 f"{ed_col}: no such editions column")
        for c in self.refresh_cols:
            if c in keys:
                raise ValueError(f"{self.name}: refresh column {c} is a key "
                                 "or period column")
            if c not in mapped and c not in cols:
                raise ValueError(f"{self.name}: refresh column {c} has no "
                                 "editions column to copy from (declare it "
                                 "or map it in refresh_from)")
        if len(set(self.refresh_cols)) != len(self.refresh_cols):
            raise ValueError(f"{self.name}: a refresh column is listed twice")
        if self.refresh_source_whole_period and (
                self.as_loaded_source_col is None
                or self.as_loaded_source_col not in mapped):
            raise ValueError(f"{self.name}: refresh_source_whole_period needs "
                             "as_loaded_source_col mapped in refresh_from")

    @property
    def compare_cols(self) -> tuple:
        """Refresh columns that are value or extra columns: live equals an
        edition when key_cols + these agree (NULL equals NULL, NULL is not
        0)."""
        data = {c for c, _ in self.value_cols} | {c for c, _ in self.extra_cols}
        return tuple(c for c in self.refresh_cols if c in data)

    @property
    def update_cols(self) -> tuple:
        """Refresh columns copied from the same-named editions column; a row
        is written when any of these differs."""
        mapped = dict(self.refresh_from)
        return tuple(c for c in self.refresh_cols if c not in mapped)

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
                   supersedes: "int | None", strict: bool = False,
                   allow_revert: bool = False) -> int:
    """Insert one edition of a period; return its edition number.

    If source_sha256 is already recorded for the period nothing is inserted
    and that edition's number is returned (a re-run is harmless). Otherwise:
    rows must be non-empty, every key column present in each row, any period
    in a row equal to `period`, and `supersedes` must be the period's current
    chain tip (None only when the period has no editions); anything else
    halts. A missing value or extra column is stored NULL, never 0, unless
    strict is true: then every row must carry every data column (spec.data_cols,
    the period included) and a row that lacks one halts before anything is
    written (S1b's original insert refused a missing column; its value and
    value_flag are both nullable, so the table alone would not catch one).

    allow_revert (opt-in, for sources whose hash identifies content rather
    than a file, e.g. S8b): the already-recorded shortcut applies only to the
    chain TIP. Same hash as the tip: nothing is inserted and the tip is
    returned. Same hash as an older edition but not the tip: the source has
    gone back to earlier content, which is a revision against the tip (RULES
    2.1), so a new edition superseding the tip is stored. False (default)
    keeps the behaviour above unchanged."""
    from psycopg2.extras import execute_values
    t, pc = spec.editions_table, spec.period_col
    if not rows:
        halt(f"insert_edition: no rows supplied for {period}")
    cur.execute(f"SELECT DISTINCT edition FROM public.{t} "
                f"WHERE {pc} = %s AND source_sha256 = %s",
                (period, source_sha256))
    existing = [r[0] for r in cur.fetchall()]
    if allow_revert:
        try:
            current = chain_tip(cur, spec, period)
        except LookupError:
            current = None
        except ValueError as e:
            halt(f"insert_edition: {t}: {e}")
        if current is not None and current in existing:
            return current
    elif existing:
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
        if strict:
            missing = [c for c in spec.data_cols if c not in r]
            if missing:
                halt(f"insert_edition: a row of {period} lacks column(s) "
                     f"{missing} (strict)")
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


# ---------------------------------------------------------------------------
# Read side and refresh
# ---------------------------------------------------------------------------

AS_LOADED_LABEL = "as loaded; published_date is the load date"
AS_LOADED_LATEST_LABEL = ("as loaded; published_date is the latest load date")


@contextmanager
def _own_savepoint(cur, name: str):
    """Run the block in a savepoint; on any exception (halt included) roll
    back to it, so the caller's transaction is as it was and still usable.
    Never commits or rolls back the caller's transaction itself."""
    cur.execute(f"SAVEPOINT {_ident(name)}")
    try:
        yield
    except BaseException:
        cur.execute(f"ROLLBACK TO SAVEPOINT {name}")
        cur.execute(f"RELEASE SAVEPOINT {name}")
        raise
    cur.execute(f"RELEASE SAVEPOINT {name}")


def latest_map(cur, spec: EditionSpec) -> tuple:
    """(tips, new_periods, chain_errors) for the periods present in the live
    table. tips: {period: chain-tip edition}; new_periods: periods with no
    editions; chain_errors: {period: message} where the chain is not one
    linear chain (fork, cycle, two roots, ...). Halts on a NULL live period
    (stricter than the originals, which would pass it on)."""
    pc = spec.period_col
    cur.execute(f"SELECT DISTINCT {pc} FROM public.{spec.live_table} "
                "ORDER BY 1")
    periods = [r[0] for r in cur.fetchall()]
    if None in periods:
        halt(f"{spec.live_table}: rows with a NULL {pc}")
    tips, new, errors = {}, [], {}
    for p in periods:
        try:
            tips[p] = chain_tip(cur, spec, p)
        except LookupError:
            new.append(p)
        except ValueError as e:
            errors[p] = str(e)
    return tips, new, errors


def rows_differing(cur, spec: EditionSpec, period: str, edition: int) -> int:
    """Rows of `period` in only one of the live table and the given edition,
    compared on key_cols + compare_cols. EXCEPT treats NULL as equal to NULL
    and NULL as different from 0 (rule 1). EXCEPT ALL, so a duplicated live
    row also counts (the originals used EXCEPT, which collapses
    duplicates)."""
    sel = ", ".join(tuple(spec.key_cols) + spec.compare_cols)
    pc, lt, et = spec.period_col, spec.live_table, spec.editions_table
    cur.execute(f"""SELECT
        (SELECT COUNT(*) FROM (SELECT {sel} FROM public.{lt} WHERE {pc} = %s
                               EXCEPT ALL SELECT {sel} FROM public.{et}
                               WHERE {pc} = %s AND edition = %s) a)
      + (SELECT COUNT(*) FROM (SELECT {sel} FROM public.{et}
                               WHERE {pc} = %s AND edition = %s
                               EXCEPT ALL SELECT {sel} FROM public.{lt}
                               WHERE {pc} = %s) b)""",
                (period, period, edition, period, edition, period))
    return cur.fetchone()[0]


def classify_period(cur, spec: EditionSpec, period: str, tip: int) -> tuple:
    """('current'|'pending'|'drift', matched edition or None).

    current: live equals the chain tip. pending: live differs from the tip
    but equals an earlier edition (a newer one is recorded, not yet
    refreshed). drift: live equals no stored edition, so it was changed
    outside the editions machinery."""
    if rows_differing(cur, spec, period, tip) == 0:
        return "current", tip
    cur.execute(f"SELECT DISTINCT edition FROM public.{spec.editions_table} "
                f"WHERE {spec.period_col} = %s AND edition <> %s "
                "ORDER BY 1 DESC", (period, tip))
    for (e,) in cur.fetchall():
        if rows_differing(cur, spec, period, e) == 0:
            return "pending", e
    return "drift", None


def live_period_counts(cur, spec: EditionSpec) -> dict:
    """{period: live rows}."""
    cur.execute(f"SELECT {spec.period_col}, COUNT(*) "
                f"FROM public.{spec.live_table} GROUP BY 1")
    return dict(cur.fetchall())


def authority_counts(cur, spec: EditionSpec) -> dict:
    """{period: distinct values of the first key column} of the live table
    (authorities, for every real spec)."""
    cur.execute(f"SELECT {spec.period_col}, COUNT(DISTINCT {spec.key_cols[0]}) "
                f"FROM public.{spec.live_table} GROUP BY 1")
    return dict(cur.fetchall())


def modal_count(counts: dict) -> "int | None":
    """Most common per-period count (the larger on a tie): the expected size
    of a period, derived from the data rather than typed in."""
    if not counts:
        return None
    c = Counter(counts.values())
    return max(c, key=lambda n: (c[n], n))


def status(cur, spec: EditionSpec) -> dict:
    """What needs action between the live table and its editions (read-only).

    new_periods:     in live with no editions (run sync-new)
    pending_refresh: live equals an earlier edition while a later one exists
                     (run refresh-latest)
    drift:           live differs from the latest edition and equals no stored
                     edition (changed outside the editions machinery)
    bad_counts:      {period: (live rows, expected)} where the live row count is
                     not spec.expected_rows_per_period(cur, period) (default:
                     the most common per-period live count), or where the
                     latest edition's row count differs from live's
    forked:          {period: message} where the supersedes chain is not one
                     linear chain (a fork, or any other broken chain; S1 called
                     this chain_errors)
    periods:         number of live periods
    ok is true only when all five are empty."""
    tips, new, errors = latest_map(cur, spec)
    drift, pending = [], []
    for p, tip in tips.items():
        kind, _ = classify_period(cur, spec, p, tip)
        if kind == "drift":
            drift.append(p)
        elif kind == "pending":
            pending.append(p)
    counts = live_period_counts(cur, spec)
    mode = modal_count(counts)
    bad_counts = {}
    for p, n in sorted(counts.items()):
        want = None
        if spec.expected_rows_per_period is not None:
            want = spec.expected_rows_per_period(cur, p)
        if want is None:
            want = mode
        if n != want:
            bad_counts[p] = (n, want)
    for p, tip in tips.items():
        cur.execute(f"SELECT COUNT(*) FROM public.{spec.editions_table} "
                    f"WHERE {spec.period_col} = %s AND edition = %s", (p, tip))
        n = cur.fetchone()[0]
        if n != counts.get(p) and p not in bad_counts:
            bad_counts[p] = (counts.get(p), n)
    ok = not (new or drift or pending or errors or bad_counts)
    return {"new_periods": sorted(new), "pending_refresh": sorted(pending),
            "drift": sorted(drift), "bad_counts": bad_counts,
            "forked": errors, "periods": len(counts), "ok": ok}


def rows_sha256(spec: EditionSpec, rows: list) -> str:
    """sha256 of a canonical text rendering of one period's rows: one line
    per row sorted by key_cols, of key_cols then value_cols then extra_cols
    (spec order, the period left out) joined by '|', NULL (or a missing key)
    as the empty string, booleans as true/false, lines joined by LF, UTF-8.

    Equals S1's live_text_sha256 when value_cols are S1's MEASURES and there
    are no extra columns. S1b's live_rows_sha256 also renders the period and
    RO4's rows_sha256 uses REFRESH_COLS order, so a loader that must
    reproduce its historic hashes keeps its own function."""
    other = spec.data_cols[len(spec.key_cols) + 1:]

    def cell(v):
        if v is None:
            return ""
        if isinstance(v, bool):
            return "true" if v else "false"
        return str(v)
    ordered = sorted(rows, key=lambda r: tuple(str(r[k]) for k in spec.key_cols))
    lines = ["|".join([cell(r[k]) for k in spec.key_cols]
                      + [cell(r.get(c)) for c in other]) for r in ordered]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _record_as_loaded(cur, spec: EditionSpec, period: str,
                      expected: "int | None") -> int:
    """Edition 1 'as loaded' for a live period that has no editions.

    The live rows (spec.data_cols) must have unique keys, form a complete
    grid of `expected` first-key values (authorities) x the period's other
    key combinations (S1b's authorities x categories; for a single key
    simply `expected` rows), and share one loaded_at date and, if
    spec.as_loaded_source_col is set, one value of it; otherwise halt.
    published_date is the load date. S1b's category-set comparison with the
    neighbouring period and its single-source_url check, and RO4's manifest
    file match, are source-specific and stay in those loaders."""
    cols = spec.data_cols
    src = spec.as_loaded_source_col
    extra = ", (loaded_at AT TIME ZONE 'UTC')::date" + (f", {src}" if src else "")
    cur.execute(f"SELECT {', '.join(cols)}{extra} "
                f"FROM public.{spec.live_table} "
                f"WHERE {spec.period_col} = %s", (period,))
    fetched = cur.fetchall()
    recs = [dict(zip(cols, r[:len(cols)])) for r in fetched]
    kc = spec.key_cols
    keys = {tuple(r[k] for k in kc) for r in recs}
    if len(keys) != len(recs):
        halt(f"{period}: {len(recs) - len(keys)} live rows repeat a key "
             f"{tuple(kc)}")
    auths = {r[kc[0]] for r in recs}
    others = {tuple(r[k] for k in kc[1:]) for r in recs}
    if expected is not None and (len(auths) != expected
                                 or len(recs) != expected * len(others)):
        halt(f"{period}: {len(auths)} authorities and {len(recs)} live rows, "
             f"expected {expected} authorities and {expected * len(others)} "
             "rows")
    loaded = {r[len(cols)] for r in fetched}
    latest = spec.as_loaded_date == "latest"
    if not latest and len(loaded) != 1:
        halt(f"{period}: live rows differ in loaded_at ({len(loaded)} dates)")
    source_file = None
    if src:
        files = {r[len(cols) + 1] for r in fetched}
        if len(files) != 1:
            halt(f"{period}: live rows differ in {src} ({len(files)} values)")
        source_file = files.pop()
    return insert_edition(cur, spec, recs, period,
                          release_label=(AS_LOADED_LATEST_LABEL if latest
                                         else AS_LOADED_LABEL),
                          published_date=max(loaded) if latest else loaded.pop(),
                          source_file=source_file,
                          source_sha256=rows_sha256(spec, recs),
                          supersedes=None)


def sync_new(cur, spec: EditionSpec,
             expected_authorities_n: "int | None" = None) -> list:
    """Give every live period that has no editions its edition 1, 'as loaded'
    (see _record_as_loaded). Never touches a period that already has
    editions, so it is idempotent. The authority count is derived (most
    common per-period count over periods that already have editions); if
    nothing can be derived expected_authorities_n (CLI --expected-authorities
    N) must be given, and once a count can be derived N is accepted only if
    it equals it. Halts on an invalid chain anywhere or on a period that
    fails the checks; all of this call's inserts are then undone (own
    savepoint). Returns the periods given an edition, sorted.

    Authorities are counted as distinct first-key values (S1b); S1 and RO4
    counted rows, which is the same for a single key."""
    tips, new, errors = latest_map(cur, spec)
    if errors:
        halt("sync-new: invalid edition chain " + "; ".join(
            f"{p}: {m}" for p, m in errors.items()))
    known = {p: n for p, n in authority_counts(cur, spec).items()
             if p not in new}
    derived = modal_count(known)
    if (expected_authorities_n is not None and derived is not None
            and expected_authorities_n != derived):
        halt(f"sync-new: --expected-authorities {expected_authorities_n} "
             f"differs from the count {derived} derived from periods that "
             "already have editions; it is only accepted when nothing can be "
             "derived (or when it equals the derived count)")
    expected = derived if derived is not None else expected_authorities_n
    if new and expected is None:
        halt("sync-new: no live period has editions, so the authority count "
             "cannot be derived; give --expected-authorities N")
    done = []
    with _own_savepoint(cur, "editions_core_sync_new"):
        for p in sorted(new):
            _record_as_loaded(cur, spec, p, expected)
            done.append(p)
    return done


def period_hashes(cur, spec: EditionSpec, which: str,
                  exclude: tuple = ("loaded_at",)) -> dict:
    """{period: md5} of each period's content in the live table (`which` =
    'live') or the editions table ('editions'): every column except
    `exclude`, each row as jsonb text (so NULL, 0, types and column names all
    count), rows ordered by key_cols (then edition) and, as a tie-break for a
    non-unique live table, by the row text itself (the originals ordered by
    key only)."""
    if which == "live":
        table, order = spec.live_table, tuple(spec.key_cols)
    elif which == "editions":
        table, order = spec.editions_table, tuple(spec.key_cols) + ("edition",)
    else:
        raise ValueError(f"period_hashes: which must be 'live' or 'editions', "
                         f"not {which!r}")
    ex = "ARRAY[" + ", ".join(f"'{_ident(c)}'" for c in exclude) + "]::text[]"
    row = f"(to_jsonb(t) - {ex})::text"
    by = ", ".join([f"t.{c}" for c in order] + [row])
    cur.execute(f"""SELECT {spec.period_col},
        md5(string_agg({row}, E'\\n' ORDER BY {by}))
        FROM public.{table} t GROUP BY 1""")
    return dict(cur.fetchall())


def guard_problems(full_before: dict, full_after: dict, kept_before: dict,
                   kept_after: dict, intended) -> list:
    """The before/after rule of a refresh. full_*: period_hashes excluding
    loaded_at only. kept_*: period_hashes excluding the refresh columns (so
    loaded_at and every other column count). A period outside `intended`
    must be identical in full and in loaded_at (a period appearing or
    vanishing counts); a period inside it may differ only in the refresh
    columns."""
    bad = []
    for p in sorted(set(full_before) | set(full_after), key=str):
        if p in intended:
            continue
        if (full_before.get(p) != full_after.get(p)
                or kept_before.get(p) != kept_after.get(p)):
            bad.append(f"guard: {p} changed but was not being refreshed")
    for p in sorted(intended):
        if kept_before.get(p) != kept_after.get(p):
            bad.append(f"guard: {p} changed outside the refresh columns "
                       "(or its row count moved)")
    return bad


def _key_join(spec: EditionSpec) -> str:
    return " AND ".join(f"e.{c} = l.{c}" for c in
                        tuple(spec.key_cols) + (spec.period_col,))


def _refresh_where(spec: EditionSpec) -> str:
    """A live row needs writing when any update column differs (NULL-safe)."""
    if not spec.update_cols:
        return "(FALSE)"
    return "(" + " OR ".join(f"l.{c} IS DISTINCT FROM e.{c}"
                             for c in spec.update_cols) + ")"


def _one_sided(cur, spec: EditionSpec, period: str, edition: int) -> int:
    """Keys in only one of the live period and the edition."""
    lt, et, pc = spec.live_table, spec.editions_table, spec.period_col
    keys = " AND ".join(f"e.{c} = l.{c}" for c in spec.key_cols)
    cur.execute(f"""SELECT
        (SELECT COUNT(*) FROM public.{lt} l WHERE l.{pc} = %s AND NOT EXISTS
            (SELECT 1 FROM public.{et} e WHERE e.{pc} = %s
             AND e.edition = %s AND {keys}))
      + (SELECT COUNT(*) FROM public.{et} e WHERE e.{pc} = %s
            AND e.edition = %s AND NOT EXISTS
            (SELECT 1 FROM public.{lt} l WHERE l.{pc} = %s AND {keys}))""",
                (period, period, edition, period, edition, period))
    return cur.fetchone()[0]


def _plan(cur, spec: EditionSpec, accept_drift=()) -> tuple:
    """({period: {edition, kind, rows, one_sided}} for every period whose live
    rows differ from the latest edition, unaccepted drift periods). kind is
    'pending' or 'drift'; rows is how many live rows the update would write,
    one_sided how many keys are in only one of live and the edition. Halts on
    a period with no editions, an invalid chain, or an accept_drift period
    that is not drifted."""
    tips, new, errors = latest_map(cur, spec)
    if new:
        halt(f"periods with no editions {new}; run sync-new first")
    if errors:
        halt("invalid edition chain: " + "; ".join(
            f"{p}: {m}" for p, m in errors.items()))
    plan, drift = {}, []
    for p, tip in tips.items():
        kind, _ = classify_period(cur, spec, p, tip)
        if kind == "current":
            continue
        if kind == "drift" and p not in accept_drift:
            drift.append(p)
        plan[p] = {"edition": tip, "kind": kind}
    stray = sorted(set(accept_drift) - {p for p, v in plan.items()
                                        if v["kind"] == "drift"})
    if stray:
        halt(f"--accept-drift {stray}: not drifted periods, nothing to accept")
    for p, v in plan.items():
        cur.execute(f"""SELECT COUNT(*) FROM public.{spec.live_table} l
                        JOIN public.{spec.editions_table} e ON {_key_join(spec)}
                         AND e.edition = %s
                        WHERE l.{spec.period_col} = %s
                          AND {_refresh_where(spec)}""", (v["edition"], p))
        v["rows"] = cur.fetchone()[0]
        v["one_sided"] = _one_sided(cur, spec, p, v["edition"])
    return plan, drift


def _unrepairable(plan: dict) -> list:
    """Planned periods with a key in only one of live and the edition, or
    that differ with no row to update: an UPDATE cannot repair them. (RO4's
    stricter rule; S1 and S1b checked only for no row to update, and caught a
    one-sided key later, after writing, in the after-check.)"""
    return sorted(p for p, v in plan.items() if v["one_sided"] or not v["rows"])


def refresh_counts(cur, spec: EditionSpec, accept_drift=()) -> dict:
    """{period: rows refresh_latest would write} (read-only)."""
    plan, _ = _plan(cur, spec, accept_drift)
    return {p: v["rows"] for p, v in sorted(plan.items())}


def _source_cols(spec: EditionSpec) -> tuple:
    live_col = spec.as_loaded_source_col
    return live_col, dict(spec.refresh_from)[live_col]


def _set_source_whole_period(cur, spec: EditionSpec, pairs) -> None:
    """Set the live source column of every row of each refreshed period to
    the latest edition's (opt-in refresh_source_whole_period). Only that
    column; rows already right are not touched."""
    lc, ec = _source_cols(spec)
    cur.execute(f"""UPDATE public.{spec.live_table} l SET {lc} = e.{ec}
                    FROM public.{spec.editions_table} e
                    WHERE {_key_join(spec)}
                      AND (e.{spec.period_col}, e.edition) IN %s
                      AND l.{lc} IS DISTINCT FROM e.{ec}""", (pairs,))


def _source_problems(cur, spec: EditionSpec, pairs) -> list:
    """After-check: every live row of each refreshed period carries the
    latest edition's source, and the period has the same row count."""
    lc, ec = _source_cols(spec)
    bad = []
    for p, ed in pairs:
        cur.execute(f"""SELECT COUNT(*) FROM public.{spec.live_table} l
                        LEFT JOIN public.{spec.editions_table} e
                          ON {_key_join(spec)} AND e.edition = %s
                        WHERE l.{spec.period_col} = %s
                          AND l.{lc} IS DISTINCT FROM e.{ec}""", (ed, p))
        k = cur.fetchone()[0]
        if k:
            bad.append(f"{p}: {k} rows do not carry edition {ed}'s {lc} "
                       "after the refresh")
    return bad


def refresh_latest(cur, spec: EditionSpec, accept_drift: tuple = (),
                   _after_update_hook=None) -> dict:
    """Copy the latest edition into the live table for every period whose
    live rows differ from it (NULL-safe); return a result dict.

    Writes only spec.refresh_cols, on the rows of those periods that differ
    in an update column; every other column (loaded_at included) and every
    other period is untouched. In its own savepoint of the caller's
    transaction: the per-period content hash of the live table (excluding
    loaded_at, and separately excluding the refresh columns) and of the
    editions table is taken before and after. A period not being refreshed
    must be identical, a refreshed one may differ only in the refresh
    columns, the editions table must not change, the rows written must be
    the rows planned, and each refreshed period must then equal its latest
    edition on key_cols + compare_cols. Any failure rolls back to the
    savepoint and halts; the caller's transaction is otherwise untouched,
    and is never committed here. A period whose live rows equal no stored
    edition is drift: it halts unless named in accept_drift.

    _after_update_hook(cur, plan), if given, runs after the UPDATE and
    before the after-checks, inside the savepoint: a seam for tests and for
    a loader's own extra checks (S1 plugs its W1-equivalence check in here;
    the hook halts to fail the refresh). The originals called it with cur
    only. S1b's extra after-check on all live columns and S1's reproduction
    update stay in those loaders.

    Result: {'updated': {period: rows}, 'rows': n, 'drift_accepted': [...]}.
    """
    plan, drift = _plan(cur, spec, accept_drift)
    if drift:
        halt("live differs from the latest edition and matches no stored "
             f"edition for {drift}: changed outside the editions tables. "
             "Load it as an edition, or re-run with --accept-drift PERIOD to "
             "overwrite it with the latest edition")
    stuck = _unrepairable(plan)
    if stuck:
        halt(f"{stuck} differ from the latest edition in rows present in only "
             "one of them; an update of the refresh columns cannot repair that")
    result = {"updated": {}, "rows": 0,
              "drift_accepted": sorted(p for p, v in plan.items()
                                       if v["kind"] == "drift")}
    if not plan:
        return result
    refreshed = set(plan)
    sets = ", ".join([f"{c} = e.{c}" for c in spec.update_cols]
                     + [f"{lc} = e.{ec}" for lc, ec in spec.refresh_from])
    pc = spec.period_col
    pairs = tuple((p, v["edition"]) for p, v in sorted(plan.items()))
    expected = sum(v["rows"] for v in plan.values())
    with _own_savepoint(cur, "editions_core_refresh"):
        full_b = period_hashes(cur, spec, "live")
        kept_b = period_hashes(cur, spec, "live", exclude=spec.refresh_cols)
        ed_b = period_hashes(cur, spec, "editions")
        cur.execute(f"""UPDATE public.{spec.live_table} l SET {sets}
                        FROM public.{spec.editions_table} e
                        WHERE {_key_join(spec)}
                          AND (e.{pc}, e.edition) IN %s
                          AND {_refresh_where(spec)}""", (pairs,))
        n = cur.rowcount
        if spec.refresh_source_whole_period:
            _set_source_whole_period(cur, spec, pairs)
        if _after_update_hook is not None:
            _after_update_hook(cur, plan)
        full_a = period_hashes(cur, spec, "live")
        kept_a = period_hashes(cur, spec, "live", exclude=spec.refresh_cols)
        bad = guard_problems(full_b, full_a, kept_b, kept_a, refreshed)
        if period_hashes(cur, spec, "editions") != ed_b:
            bad.append("guard: the editions table changed during the refresh")
        if n != expected:
            bad.append(f"wrote {n} rows, expected {expected}")
        for p, v in sorted(plan.items()):
            k = rows_differing(cur, spec, p, v["edition"])
            if k:
                bad.append(f"{p}: {k} rows still differ from edition "
                           f"{v['edition']} after the refresh")
        if spec.refresh_source_whole_period:
            bad.extend(_source_problems(cur, spec, pairs))
        if bad:
            halt("refresh-latest failed its before/after checks, rolled back: "
                 + "; ".join(bad[:6]))
    result["updated"] = {p: v["rows"] for p, v in sorted(plan.items())}
    result["rows"] = n
    return result
