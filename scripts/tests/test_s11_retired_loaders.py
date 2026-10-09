"""The retired S11 scripts (scripts/historical/) stop at once, also when run
directly (scripts/ is then not on sys.path) and with a broken environment."""

import os
import subprocess
import sys
import unittest
from pathlib import Path

HIST = Path(__file__).resolve().parents[1] / "historical"
NAMES = ("s11_cqc_fetch.py", "s11_cqc_process.py", "s11_cqc_map.py",
         "s11_cqc_load.py", "s11_cqc_verify.py")


class RetiredS11(unittest.TestCase):
    def _run(self, name, env=None):
        return subprocess.run([sys.executable, str(HIST / name)],
                              capture_output=True, text=True, cwd=HIST,
                              timeout=60, env=env)

    def test_run_directly_prints_retired_and_exits_nonzero(self):
        for name in NAMES:
            with self.subTest(script=name):
                r = self._run(name)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("RETIRED", r.stderr)
                self.assertIn("s11_cqc_editions.py", r.stderr)
                self.assertIn("ON CONFLICT (location_id) DO UPDATE", r.stderr)
                self.assertIn("loaded_at", r.stderr)
                self.assertIn("July and August 2026", r.stderr)
                self.assertIn("cqc_locations_legacy", r.stderr)
                self.assertIn("kept their earlier values", r.stderr)
                self.assertNotIn("survive only", r.stderr)
                self.assertNotIn("ModuleNotFoundError", r.stderr)
                self.assertNotIn("Traceback", r.stderr)

    def test_step_specific_wording(self):
        self.assertIn("same-named download", self._run("s11_cqc_fetch.py").stderr)
        self.assertIn("file name", self._run("s11_cqc_fetch.py").stderr)
        err = self._run("s11_cqc_map.py").stderr
        self.assertIn("cqc_unresolved_locations", err)
        self.assertIn("no preview", err)

    def test_retired_even_with_broken_environment(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(HIST / "nonexistent")
        for k in ("PGHOST", "PG_PASSWORD", "PGPASSWORD", "DATABASE_URL"):
            env.pop(k, None)
        for name in NAMES:
            with self.subTest(script=name):
                r = self._run(name, env)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("RETIRED", r.stderr)
                self.assertNotIn("Traceback", r.stderr)

    def test_old_scripts_no_longer_in_live_folder(self):
        for name in NAMES:
            self.assertFalse((HIST.parent / name).exists(), name)


if __name__ == "__main__":
    unittest.main()
