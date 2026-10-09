"""The retired S18 scripts (scripts/historical/s18_pipr_*.py) stop at once,
also when run directly (scripts/ is then not on sys.path)."""

import subprocess
import sys
import unittest
from pathlib import Path

HIST = Path(__file__).resolve().parents[1] / "historical"
NAMES = ["s18_pipr_transform.py", "s18_pipr_load.py", "s18_pipr_verify.py",
         "s18_pipr_fetch.py"]


class RetiredS18(unittest.TestCase):
    def test_run_directly_prints_retired_and_exits_nonzero(self):
        for name in NAMES:
            with self.subTest(script=name):
                r = subprocess.run([sys.executable, str(HIST / name)],
                                   capture_output=True, text=True, cwd=HIST,
                                   timeout=60)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("RETIRED", r.stderr)
                self.assertIn("s18_pipr_editions.py", r.stderr)
                self.assertIn("ON CONFLICT DO NOTHING", r.stderr)
                self.assertNotIn("ModuleNotFoundError", r.stderr)
                self.assertNotIn("Traceback", r.stderr)

    def test_old_scripts_no_longer_in_scripts_folder(self):
        top = HIST.parent
        for name in NAMES:
            self.assertFalse((top / name).exists(), name)


if __name__ == "__main__":
    unittest.main()
