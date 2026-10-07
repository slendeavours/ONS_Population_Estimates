import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from loader_baseline import compare  # noqa: E402

BEFORE = {"tables": {"t": {"rows": 3, "sha256": "aaa"}},
          "commands": {"s1_verify": {"exit_code": 0, "lines": ["PASS x"], "line_count": 1}}}


class T(unittest.TestCase):
    def test_compare_reports_changed_hash(self):
        after = {"tables": {"t": {"rows": 3, "sha256": "bbb"}},
                 "commands": BEFORE["commands"]}
        out = compare(BEFORE, after)
        self.assertEqual(len(out), 1)
        self.assertIn("tables.t.sha256", out[0])

    def test_compare_identical_is_empty(self):
        self.assertEqual(compare(BEFORE, dict(BEFORE)), [])


if __name__ == "__main__":
    unittest.main()
