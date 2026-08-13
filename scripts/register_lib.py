"""Shared, publishable helpers for reading the source register.

Split out of source_register_audit.py so that the audit — which enumerates
table names, and so carries a counterparty name for the private S20 source —
can live outside any git working tree while the parts that are safe to
publish stay in the repo.

Nothing in this module names a table.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _publish_root(repo):
    """Locate the published tree, whichever checkout this is running in.

    This module is published, so it runs in two layouts. In the outer working
    copy the published tree is a subdirectory; inside the published checkout
    itself it is the repository root. Resolving to the subdirectory
    unconditionally pointed METHODOLOGY at a path that does not exist there,
    which broke every caller in that tree.

    The nested tree is tested first, and that order is load-bearing. The outer
    working copy carries its own docs/METHODOLOGY.md as well, so a check of
    "does REPO/docs/METHODOLOGY.md exist" answers yes in both layouts and
    would resolve the outer tree to its own stale copy instead of the
    published one.
    """
    nested = repo / "ONS_Population_Estimates"
    if (nested / "docs" / "METHODOLOGY.md").exists():
        return nested
    if (repo / "docs" / "METHODOLOGY.md").exists():
        return repo
    raise SystemExit(
        f"HALT: docs/METHODOLOGY.md not found under {repo} or {nested}. "
        f"It is the source register and nothing here works without it.")


PUBLISH = _publish_root(REPO)
METHODOLOGY = PUBLISH / "docs" / "METHODOLOGY.md"
INDEX_HTML = PUBLISH / "index.html"

# staging_la_signals column prefix -> source number, for the wiring check.
SIGNAL_SOURCE = [
    ("ta_", "1"), ("ro4_", "2"), ("population", "3"),
    ("care_leavers_", "4"), ("imd_", "5"),
    ("hb_sa_", "8"), ("drd_", "9a"), ("crfd_", "9b"),
    ("rough_sleeping_", "10"), ("supported_living_", "11"),
    ("efs_flag", "12"), ("s114_flag", "12"), ("housing_register", "13"),
    ("lha_", "14"), ("marac_", "17"), ("pip_", "19"), ("ctb_", "22"),
]

# Map layer fields that are NOT staging_la_signals columns. S15 reaches the
# map through its own hpi_la_prices.json, so it has a layer and no staging
# column — the mirror image of S19, which has staging columns and no layer.
NON_SIGNAL_LAYER_SOURCE = {
    "avg_price_all": "15",
    "hb_sa_claimants_latest": "8b",
}


def parse_register():
    """Rows of the METHODOLOGY source register table."""
    text = METHODOLOGY.read_text(encoding="utf-8")
    rows, in_table = {}, False
    for line in text.splitlines():
        if line.startswith("| S# | Source |"):
            in_table = True
            continue
        if in_table:
            if not line.startswith("|"):
                break
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not cells or set(cells[0]) <= set("-: "):
                continue
            rows[cells[0]] = {"source": cells[1], "metrics": cells[2],
                              "publisher": cells[3] if len(cells) > 3 else "",
                              "frequency": cells[4] if len(cells) > 4 else ""}
    return rows


def map_layer_fields():
    html = INDEX_HTML.read_text(encoding="utf-8")
    block = re.search(r"var LAYERS = \[(.*?)\n\];", html, re.S)
    if not block:
        sys.exit("HALT: could not find the LAYERS array in index.html")
    return re.findall(r"field:\s*'([^']+)'", block.group(1))


def source_for_signal_column(col, allow_non_signal=False):
    if allow_non_signal and col in NON_SIGNAL_LAYER_SOURCE:
        return NON_SIGNAL_LAYER_SOURCE[col]
    for prefix, src in SIGNAL_SOURCE:
        if col == prefix or col.startswith(prefix):
            return src
    return None


def register_sort_key(s):
    return (int(re.match(r"\d+", s).group()), s)
