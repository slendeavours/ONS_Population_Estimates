"""The four retired S22 scripts (scripts/historical/s22_*.py) stop at once, also
when run directly (scripts/ is then not on sys.path) and with a broken
environment."""

import os
import subprocess
import sys
import unittest
from pathlib import Path

HIST = Path(__file__).resolve().parents[1] / "historical"
NAMES = ["s22_ctb_discover.py", "s22_ctb_empties_build.py",
         "s22_run.py", "s22_verify.py"]


class RetiredS22(unittest.TestCase):
    def _run(self, name, env=None):
        return subprocess.run([sys.executable, str(HIST / name)],
                              capture_output=True, text=True, cwd=HIST,
                              timeout=60, env=env)

    def _broken_env(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(HIST / "nonexistent")
        for k in ("PGHOST", "PG_PASSWORD", "PGPASSWORD", "DATABASE_URL",
                  "PG_PORT", "PG_DATABASE", "PG_USER"):
            env.pop(k, None)
        return env

    def test_run_directly_prints_retired_and_exits_nonzero(self):
        for name in NAMES:
            with self.subTest(name):
                r = self._run(name)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("RETIRED", r.stderr)
                self.assertIn("s22_ctb_editions.py", r.stderr)
                self.assertIn("2026-08-13", r.stderr)
                self.assertIn("id 83", r.stderr)
                self.assertNotIn("ModuleNotFoundError", r.stderr)
                self.assertNotIn("Traceback", r.stderr)

    def test_retired_even_with_broken_environment(self):
        for name in NAMES:
            with self.subTest(name):
                r = self._run(name, self._broken_env())
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("RETIRED", r.stderr)
                self.assertIn("s22_ctb_editions.py", r.stderr)
                self.assertNotIn("Traceback", r.stderr)

    def test_wording_names_what_each_step_did(self):
        want = {
            "s22_ctb_empties_build.py": ["ON CONFLICT", "DO UPDATE", "loaded_at",
                                         "no preview", "as 0"],
            "s22_run.py": ["no preview"],
            "s22_verify.py": ["run-log row"],
            "s22_ctb_discover.py": ["same-named raw download"],
        }
        for name, bits in want.items():
            r = self._run(name)
            for b in bits:
                with self.subTest(name=name, bit=b):
                    self.assertIn(b, r.stderr)

    def test_guard_comes_before_any_import_that_can_fail(self):
        for name in NAMES:
            src = (HIST / name).read_text(encoding="utf-8")
            guard = src.index("sys.exit(_RETIRED)")
            for mod in ("openpyxl", "pandas", "psycopg2", "_db", "s22_ctb_"):
                for stmt in ("import " + mod, "from " + mod):
                    # the retired message names the scripts, so look at
                    # import statements at the start of a line only
                    for line_start in ("\n" + stmt,):
                        if line_start in src:
                            with self.subTest(name=name, mod=mod):
                                self.assertLess(guard, src.index(line_start))

    def test_old_scripts_no_longer_in_scripts_folder(self):
        for name in NAMES:
            self.assertFalse((HIST.parent / name).exists(), name)
            self.assertTrue((HIST / name).exists(), name)


if __name__ == "__main__":
    unittest.main()
