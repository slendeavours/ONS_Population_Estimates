"""The retired S4 rebuild (scripts/historical/s4_rebuild_care_leavers.py) stops
at once, also when run directly (scripts/ is then not on sys.path) and with a
broken environment."""

import os
import subprocess
import sys
import unittest
from pathlib import Path

HIST = Path(__file__).resolve().parents[1] / "historical"
NAME = "s4_rebuild_care_leavers.py"


class RetiredS4(unittest.TestCase):
    def _run(self, env=None):
        return subprocess.run([sys.executable, str(HIST / NAME)],
                              capture_output=True, text=True, cwd=HIST,
                              timeout=60, env=env)

    def test_run_directly_prints_retired_and_exits_nonzero(self):
        r = self._run()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("RETIRED", r.stderr)
        self.assertIn("s4_care_leaver_editions.py", r.stderr)
        self.assertIn("ON CONFLICT", r.stderr)
        self.assertIn("DO UPDATE", r.stderr)
        self.assertIn("loaded_at", r.stderr)
        self.assertIn("2026-08-20", r.stderr)
        self.assertIn("v or 0", r.stderr)
        self.assertIn("la_code_lookup", r.stderr)
        self.assertIn("Bournemouth and Poole 2019", r.stderr)
        self.assertIn("care_leaver_accommodation_bak_20260820", r.stderr)
        self.assertNotIn("ModuleNotFoundError", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_retired_even_with_broken_environment(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(HIST / "nonexistent")
        for k in ("PGHOST", "PG_PASSWORD", "PGPASSWORD", "DATABASE_URL",
                  "PG_PORT", "PG_DATABASE", "PG_USER"):
            env.pop(k, None)
        r = self._run(env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("RETIRED", r.stderr)
        self.assertIn("s4_care_leaver_editions.py", r.stderr)
        self.assertIn("ON CONFLICT", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_guard_comes_before_any_import_that_can_fail(self):
        src = (HIST / NAME).read_text(encoding="utf-8")
        guard = src.index("sys.exit(_RETIRED)")
        for mod in ("pandas", "psycopg2", "dotenv"):
            self.assertLess(guard, src.index("import " + mod) if "import " + mod in src
                            else src.index("from " + mod), mod)
        self.assertLess(guard, src.index("load_dotenv("))

    def test_old_script_no_longer_in_verify_folder(self):
        verify = HIST.parent / "verify"
        self.assertFalse((verify / "rebuild_care_leavers.py").exists())


if __name__ == "__main__":
    unittest.main()
