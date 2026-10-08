"""The eight Workflow 1 steps, as SQL files in sql/w1, in execution order.

Each file is the n8n node's query verbatim. Steps that take the run id use
the n8n placeholder $1 (n8n passed it via queryReplacement); the runner turns
it into a named parameter, as s22_w1_wire._pg does.
"""
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

W1_SQL_DIR = Path(__file__).resolve().parents[1] / "sql" / "w1"
MANIFEST_NAME = "MANIFEST.json"


@dataclass(frozen=True)
class Step:
    name: str
    filename: str
    uses_run_id: bool


STEPS = (
    Step("Create Staging Tables", "01_create_staging_tables.sql", False),
    Step("Signal Column Pre-flight", "02_signal_column_preflight.sql", False),
    Step("Create Run", "03_create_run.sql", False),
    Step("National Aggregates", "04_national_aggregates.sql", True),
    Step("LA Signals", "05_la_signals.sql", True),
    Step("Tenant Type Rankings", "06_tenant_type_rankings.sql", True),
    Step("Section 3 Top 3 LAs", "07_section_3_top_3_las.sql", True),
    Step("Mark Run Complete", "08_mark_run_complete.sql", True),
)


def read_step(step: Step) -> str:
    with open(W1_SQL_DIR / step.filename, encoding="utf-8", newline="") as f:
        return f.read()


def normalised_sha256(sql: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", sql).strip()
                          .encode("utf-8")).hexdigest()


def load_manifest() -> dict:
    """filename -> normalised sha256. Each hash sits on its own
    `"sha256": "..."` line, the one shape the credential scan allows."""
    with open(W1_SQL_DIR / MANIFEST_NAME, encoding="utf-8") as f:
        return {name: entry["sha256"] for name, entry in json.load(f).items()}
