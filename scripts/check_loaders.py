"""Loader conformance checker: does each loader meet the loader standard?

    python scripts/check_loaders.py

Prints a table with one row per source: PASS, or the reasons it does not
conform. It reports and always exits 0; it is not a gate yet. The FAIL list is
the worklist for bringing the remaining loaders onto the shared core.

The checks are text and AST heuristics, not proof. They catch the usual way
a loader departs from the standard and can be fooled in both directions
(a write path built dynamically, a zero coercion spelled unusually, a
`--commit` option that is parsed but ignored). A PASS means "nothing the
heuristics look for", and the verify script is still the real test.

Checks (see docs/RULES.md, sections 1 and 5):
  a. Preview by default: the file has a `--commit` option, and every
     `.commit()` call sits under an `if` that mentions commit/simulate. A
     loader with write SQL and no `--commit` fails.
  b. No zero coercion of source values: `or 0`, `|| 0`, `COALESCE(x, 0)`,
     `else 0`, `, 0)`. A line ending `# not a source value` is allowed (for
     derived sums).
  c. Imports `editions_core`, or declares `NO_EDITIONS = "<reason>"`.
  d. A verify script exists: `<stem>_verify.py`, `verify_<stem>.py`, or the
     same with a trailing `_build`/`_load`/`_editions`/`_refresh` removed
     from the stem, or `verify_[load_]<name>.py` with the `sN_` prefix
     removed (verify_load_drd.py for s9a_drd_build.py), beside the loader or
     in scripts/verify.
  e. A source the registry marks `revises_back_series` must import
     `editions_core` (a NO_EDITIONS declaration does not excuse it).
  f. No private Barnsley/Sheffield recode dict (docs/RULES.md rule 4): a
     loader outside scripts/historical/ with a dict literal keyed by
     E08000038 or E08000039 fails; it is to use scripts/geography.py
     (resolve/canonical). A file listed in GEOGRAPHY_PENDING is reported as
     a note, not a failure, until it is migrated.
"""
import ast
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent

# source code -> loader file(s), relative to scripts/. Explicit because the
# registry build_script_path is blank for S1, S2 and others. The key is the
# display code ('S8b'); the registry's own source_code is 'S' removed and
# lower-cased ('8b'), see registry_code().
LOADERS = {
    "S1": ["s1_editions.py"],
    "S1b": ["s1b_editions.py"],
    "S2": ["ro4_editions.py"],
    "S3": ["s3_mye_refresh.py"],
    "S4": ["verify/rebuild_care_leavers.py"],
    "S6": ["s6_asylum_build.py"],
    "S8b": ["s8b_hb_editions.py"],
    "S9a": ["s9a_drd_build.py"],
    "S9b": ["s9b_crfd_build.py"],
    "S11": ["s11_cqc_load.py"],
    "S14": ["s14_lha_rates_build_v2.py"],
    "S15": ["s15_hpi_editions.py"],
    "S18": ["s18_pipr_editions.py"],
    "S19": ["s19_pip_editions.py"],
    "S21": ["s21_statistical_neighbours_build.py"],
    "S22": ["s22_ctb_empties_build.py"],
    "S23": ["s23_rsh_stock_build.py"],
    "S24": ["s24_rsh_register_build.py"],
}

ZERO_PATTERNS = [
    (re.compile(r"\bor\s+0\b(?![\w.\-])"), "or 0"),
    (re.compile(r"\|\|\s*0\b"), "|| 0"),
    (re.compile(r"coalesce\s*\([^;]*?,\s*0\s*\)", re.I), "COALESCE(x, 0)"),
    (re.compile(r"\belse\s+0\b(?!\.\d)", re.I), "else 0"),
    # `, 0)` as a default; not a counter increment `.get(k, 0) + 1` or a
    # `(0, 0)` tuple default.
    (re.compile(r"(?<!\(0)(?<!, 0),\s*0\s*\)(?!\s*[+\-]\s*\w)"), ", 0)"),
]
ALLOW = "# not a source value"
RECODE_KEYS = {"E08000038", "E08000039"}
# Loaders that conformed before check f existed and still carry a private
# recode dict: reported as a note (no PASS changed when the check was added).
# Remove the entry when the loader moves to geography.resolve.
GEOGRAPHY_PENDING = {
    "ro4_editions.py": "RAW_RENAMED in raw_cells (the independent raw "
                       "re-read); move to geography.py when RO4 is next "
                       "migrated",
}
WRITE_SQL = re.compile(r"\b(insert\s+into|update\s+\w+\s+set|delete\s+from|"
                       r"truncate|create\s+table|drop\s+table)\b", re.I)


def _zero_coercions(text):
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        if line.rstrip().endswith(ALLOW) or line.lstrip().startswith("#"):
            continue
        for rx, label in ZERO_PATTERNS:
            if rx.search(line):
                hits.append((n, label))
                break
    return hits


def _imports_core(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name.split(".")[-1] == "editions_core" for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[-1] == "editions_core":
                return True
    return False


def _unguarded_commits(tree):
    """Line numbers of .commit() calls not under an `if` that mentions commit
    or simulate. Heuristic: a guard can also be a helper's own test."""
    bad = []

    def walk(node, guarded):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # A deliberate DDL/schema subcommand is its own explicit opt-in.
            n = node.name.lower()
            guarded = guarded or "ddl" in n or "schema" in n
        if isinstance(node, ast.If):
            src = ast.dump(node.test).lower()
            g = guarded or "commit" in src or "simulate" in src
            for ch in node.body:
                walk(ch, g)
            for ch in node.orelse:
                walk(ch, guarded)
            return
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "commit" and not guarded):
            bad.append(node.lineno)
        for ch in ast.iter_child_nodes(node):
            walk(ch, guarded)

    walk(tree, False)
    return bad


def _recode_dicts(tree):
    """Line numbers of dict literals keyed by E08000038 or E08000039."""
    return sorted(node.lineno for node in ast.walk(tree)
                  if isinstance(node, ast.Dict)
                  and any(isinstance(k, ast.Constant) and k.value in RECODE_KEYS
                          for k in node.keys))


def _historical(path):
    return "historical" in Path(path).parts


def geography_notes(path):
    """Notes (not failures) for a GEOGRAPHY_PENDING loader that still carries
    a private recode dict."""
    path = Path(path)
    if path.name not in GEOGRAPHY_PENDING or _historical(path):
        return []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return []
    lines = _recode_dicts(tree)
    if not lines:
        return []
    return [f"note: private Barnsley/Sheffield recode dict (line {lines[0]}) "
            f"pending geography.py: {GEOGRAPHY_PENDING[path.name]}"]


def _verify_exists(path):
    stem = path.stem
    base = re.sub(r"_(build_v2|build|load|editions|refresh)$", "", stem)
    core = re.sub(r"^s\d+[a-z]?_", "", base)
    names = {f"verify_load_{core}.py", f"verify_{core}.py"}
    for s in {stem, base}:
        names.update({f"{s}_verify.py", f"verify_{s}.py"})
    dirs = [path.parent, path.parent / "verify", SCRIPTS / "verify"]
    return any((d / n).exists() for d in dirs for n in names)


def check_loader(path, registry_row):
    """Return plain-language reasons the loader fails the standard; [] = conforms."""
    path = Path(path)
    text = path.read_text(encoding="utf-8", errors="replace")
    reasons = []
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        return [f"could not be parsed ({e.msg}, line {e.lineno})"]

    if "--commit" not in text:
        if WRITE_SQL.search(text) or ".commit(" in text:
            reasons.append("writes to the database but has no --commit option "
                           "(not preview by default)")
        else:
            reasons.append("has no --commit option")
    else:
        lines = _unguarded_commits(tree)
        if lines:
            reasons.append("commits without a commit/simulate guard "
                           f"(line {lines[0]})")

    zero = _zero_coercions(text)
    if zero:
        n, label = zero[0]
        more = f" and {len(zero) - 1} more" if len(zero) > 1 else ""
        reasons.append(f"possible zero coercion of a source value ({label}, "
                       f"line {n}{more})")

    core = _imports_core(tree)
    declared = re.search(r"^NO_EDITIONS\s*=\s*[\"'].+[\"']", text, re.M)
    if not core and not declared:
        reasons.append("does not import editions_core and declares no NO_EDITIONS reason")

    if registry_row.get("revises_back_series") and not core:
        reasons.append("source revises back series but the loader does not use editions_core")

    if not _verify_exists(path):
        reasons.append("no verify script found beside it")

    if not _historical(path) and path.name not in GEOGRAPHY_PENDING:
        lines = _recode_dicts(tree)
        if lines:
            more = f" and {len(lines) - 1} more" if len(lines) > 1 else ""
            reasons.append("carries its own Barnsley/Sheffield recode dict "
                           f"(E08000038, line {lines[0]}{more}) instead of "
                           "using scripts/geography.py (resolve/canonical)")
    return reasons


def _registry():
    from _db import get_readonly_conn
    conn = get_readonly_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT source_code, revises_back_series, build_script_path "
                    "FROM source_registry")
        return {r[0]: {"revises_back_series": bool(r[1]),
                       "build_script_path": r[2] or ""} for r in cur.fetchall()}
    finally:
        conn.close()


def registry_code(code):
    """Display code 'S8b' -> the registry's own source_code '8b'."""
    return code[1:].lower() if code[:1] in ("S", "s") else code.lower()


def main():
    reg = _registry()
    rows = []
    notes = {}
    for code, files in LOADERS.items():
        row = reg.get(registry_code(code), {"revises_back_series": False, "build_script_path": ""})
        reasons = []
        for f in files:
            p = SCRIPTS / f
            if not p.exists():
                reasons.append(f"{f}: file not found")
            else:
                reasons += check_loader(p, row)
                notes.setdefault(code, []).extend(geography_notes(p))
        rows.append((code, files[0], row["build_script_path"], reasons))
    print(f"{'source':<6} {'loader':<40} {'registry path':<14} result")
    for code, f, bsp, reasons in rows:
        blank = "BLANK" if not bsp else "set"
        print(f"{code:<6} {f:<40} {blank:<14} {'PASS' if not reasons else 'FAIL'}")
        for r in reasons:
            print(f"{'':<6}   - {r}")
        for n in notes.get(code, []):
            print(f"{'':<6}   ({n})")
    passed = sum(1 for r in rows if not r[3])
    print(f"\n{passed} of {len(rows)} loaders conform. "
          f"{sum(1 for r in rows if not r[2])} have a blank registry build_script_path.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
