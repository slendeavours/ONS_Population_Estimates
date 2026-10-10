"""W1 now runs from the repo (sql/w1, scripts/w1_run.py). No live script may
read it from n8n's database."""
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
MARKERS = ("workflow_entity", "IrrglXLYcphSg5bC")
ALLOW = {
    # Patches the LLM report workflow, a different n8n workflow from W1.
    "report_workflow_national_ta.py",
    # The S1 loaders retired from n8n; edits the S1 workflow row.
    "s1_n8n_retire_loaders.py",
    # Retires the seven n8n loader workflows for the no-loader sources
    # (none of them is W1); edits those workflow rows.
    "n8n_retire_no_loader_sources.py",
    # Archives the old n8n W1 workflow (this is its whole purpose).
    "archive_n8n_w1.py",
}


class NoN8nW1Dependency(unittest.TestCase):
    def test_no_script_reads_w1_from_n8n(self):
        offenders = []
        for p in sorted(SCRIPTS.rglob("*.py")):
            rel = p.relative_to(SCRIPTS)
            if rel.parts[0] == "historical" or p.name in ALLOW:
                continue
            if p.resolve() == Path(__file__).resolve():
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            hits = [m for m in MARKERS if m in text]
            if hits:
                offenders.append(f"{rel.as_posix()}: {', '.join(hits)}")
        self.assertEqual(offenders, [], "scripts still read W1 from n8n")


if __name__ == "__main__":
    unittest.main()
