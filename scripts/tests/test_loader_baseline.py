import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import loader_baseline  # noqa: E402
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

    def test_stderr_tail_ignored_by_compare(self):
        a = {"commands": {"x": {"exit_code": 1, "stderr_tail": ["boom"]}}}
        b = {"commands": {"x": {"exit_code": 1}}}
        self.assertEqual(compare(a, b), [])
        b["commands"]["x"]["exit_code"] = 0
        self.assertEqual(len(compare(a, b)), 1)

    def test_timeout_halts_naming_command(self):
        loader_baseline.COMMANDS  # sanity
        slow = Path(__file__).with_name("_slow_stub.py")
        slow.write_text("import time; time.sleep(30)")
        try:
            orig = loader_baseline.SCRIPTS
            loader_baseline.SCRIPTS = slow.parent
            with self.assertRaises(SystemExit) as cm:
                loader_baseline.run_command(["_slow_stub.py"], timeout=1)
            self.assertIn("_slow_stub.py", str(cm.exception))
        finally:
            loader_baseline.SCRIPTS = orig
            slow.unlink()


if __name__ == "__main__":
    unittest.main()
