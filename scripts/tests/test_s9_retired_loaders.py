"""The retired S9 scripts (scripts/historical/) stop at once, also when run
directly (scripts/ is then not on sys.path) and with a broken environment."""

import os
import subprocess
import sys
import unittest
from pathlib import Path

HIST = Path(__file__).resolve().parents[1] / "historical"
NEW = {
    "s9a_drd_build.py": "s9a_drd_editions.py",
    "verify_load_drd.py": "s9a_drd_editions.py",
    "s9b_crfd_build.py": "s9b_crfd_editions.py",
    "verify_load_crfd.py": "s9b_crfd_editions.py",
}


class RetiredS9(unittest.TestCase):
    def _run(self, name, env=None):
        return subprocess.run([sys.executable, str(HIST / name)],
                              capture_output=True, text=True, cwd=HIST,
                              timeout=60, env=env)

    def test_run_directly_prints_retired_and_exits_nonzero(self):
        for name, new in NEW.items():
            with self.subTest(script=name):
                r = self._run(name)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("RETIRED", r.stderr)
                self.assertIn(new, r.stderr)
                self.assertIn("ON CONFLICT DO UPDATE", r.stderr)
                if name.endswith("_build.py"):
                    self.assertIn("loaded_at", r.stderr)
                else:
                    self.assertIn("2026-08-20", r.stderr)
                self.assertNotIn("ModuleNotFoundError", r.stderr)
                self.assertNotIn("Traceback", r.stderr)

    def test_retired_even_with_broken_environment(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(HIST / "nonexistent")
        for k in ("PGHOST", "PGPASSWORD", "DATABASE_URL"):
            env.pop(k, None)
        for name in NEW:
            with self.subTest(script=name):
                r = self._run(name, env)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("RETIRED", r.stderr)
                self.assertNotIn("Traceback", r.stderr)

    def test_old_scripts_no_longer_in_live_folders(self):
        top = HIST.parent
        for name in ("s9a_drd_build.py", "s9b_crfd_build.py"):
            self.assertFalse((top / name).exists(), name)
        for name in ("verify_load_drd.py", "verify_load_crfd.py"):
            self.assertFalse((top / "verify" / name).exists(), name)


if __name__ == "__main__":
    unittest.main()
