"""W1 step files in sql/w1 match their manifest and the n8n step order.

Reads only the repo's own SQL files; no database is touched.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import w1_steps  # noqa: E402

N8N_ORDER = ["Create Staging Tables", "Signal Column Pre-flight", "Create Run",
             "National Aggregates", "LA Signals", "Tenant Type Rankings",
             "Section 3 Top 3 LAs", "Mark Run Complete"]


class W1StepsTest(unittest.TestCase):
    def test_steps_are_in_n8n_connection_order(self):
        self.assertEqual([s.name for s in w1_steps.STEPS], N8N_ORDER)

    def test_every_step_file_exists_and_is_non_empty(self):
        for s in w1_steps.STEPS:
            p = w1_steps.W1_SQL_DIR / s.filename
            self.assertTrue(p.is_file(), f"{s.filename} is missing")
            self.assertTrue(w1_steps.read_step(s).strip(),
                            f"{s.filename} is empty")

    def test_manifest_hash_matches_each_file(self):
        manifest = w1_steps.load_manifest()
        for s in w1_steps.STEPS:
            self.assertIn(s.filename, manifest, f"{s.filename} not in manifest")
            self.assertEqual(
                w1_steps.normalised_sha256(w1_steps.read_step(s)),
                manifest[s.filename],
                f"hash mismatch for {s.filename}")

    def test_run_id_steps_use_placeholder(self):
        by_name = {s.name: s for s in w1_steps.STEPS}
        for name in N8N_ORDER[5:]:
            self.assertTrue(by_name[name].uses_run_id, name)
        for s in w1_steps.STEPS:
            self.assertEqual("$1" in w1_steps.read_step(s), s.uses_run_id,
                             f"uses_run_id disagrees with {s.filename}")

    def test_normalised_sha256_ignores_whitespace(self):
        self.assertEqual(w1_steps.normalised_sha256("SELECT  1\n,\t2 "),
                         w1_steps.normalised_sha256("SELECT 1 , 2"))
        self.assertNotEqual(w1_steps.normalised_sha256("SELECT 1"),
                            w1_steps.normalised_sha256("SELECT 2"))


if __name__ == "__main__":
    unittest.main()
