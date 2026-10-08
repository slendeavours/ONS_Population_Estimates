"""The retired S15 loader (scripts/historical/s15_hpi_build.py) stops at once,
also when run directly (scripts/ is then not on sys.path)."""

import subprocess
import sys
import unittest
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parents[1] / "historical"
          / "s15_hpi_build.py")


class RetiredLoader(unittest.TestCase):
    def test_run_directly_prints_retired_and_exits_nonzero(self):
        r = subprocess.run([sys.executable, str(SCRIPT)],
                           capture_output=True, text=True, cwd=SCRIPT.parent,
                           timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("RETIRED", r.stderr)
        self.assertIn("s15_hpi_editions.py", r.stderr)
        self.assertNotIn("ModuleNotFoundError", r.stderr)


if __name__ == "__main__":
    unittest.main()
