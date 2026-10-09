"""The two retired S23 scripts (scripts/historical/s23_*.py) stop at once, also
when run directly (scripts/ is then not on sys.path) and with a broken
environment."""

import os
import subprocess
import sys
import unittest
from pathlib import Path

HIST = Path(__file__).resolve().parents[1] / "historical"
NAMES = ["s23_rsh_stock_build.py", "s23_rsh_stock_verify.py"]


class RetiredS23(unittest.TestCase):
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
                self.assertIn("s23_rsh_stock_editions", r.stderr)
                self.assertIn("2026-08-14", r.stderr)
                self.assertIn("id 95", r.stderr)
                self.assertNotIn("ModuleNotFoundError", r.stderr)
                self.assertNotIn("Traceback", r.stderr)

    def test_retired_even_with_broken_environment(self):
        for name in NAMES:
            with self.subTest(name):
                r = self._run(name, self._broken_env())
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("RETIRED", r.stderr)
                self.assertIn("s23_rsh_stock_editions", r.stderr)
                self.assertNotIn("Traceback", r.stderr)

    def test_each_names_its_replacement(self):
        want = {
            "s23_rsh_stock_build.py": "scripts/s23_rsh_stock_editions.py",
            "s23_rsh_stock_verify.py":
                "scripts/s23_rsh_stock_editions_verify.py",
        }
        for name, repl in want.items():
            with self.subTest(name):
                r = self._run(name)
                self.assertIn("use " + repl, r.stderr)

    def test_wording_names_what_each_step_did(self):
        want = {
            "s23_rsh_stock_build.py": ["ON CONFLICT", "DO UPDATE", "loaded_at",
                                       "no preview", "same-named raw download",
                                       "hard-coded release page",
                                       "blank stock cell as 0"],
            "s23_rsh_stock_verify.py": ["seven-gate", "rolled back",
                                        "wrote nothing"],
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
            for mod in ("openpyxl", "pandas", "psycopg2", "_db",
                        "s23_rsh_stock_"):
                for stmt in ("import " + mod, "from " + mod):
                    line_start = "\n" + stmt
                    if line_start in src:
                        with self.subTest(name=name, mod=mod):
                            self.assertLess(guard, src.index(line_start))

    def test_only_sys_is_imported_before_the_guard(self):
        for name in NAMES:
            src = (HIST / name).read_text(encoding="utf-8")
            head = src[:src.index("sys.exit(_RETIRED)")]
            imports = [ln for ln in head.splitlines()
                       if ln.startswith(("import ", "from "))]
            with self.subTest(name):
                self.assertEqual(imports, ["import sys"])

    def test_old_scripts_no_longer_in_scripts_folder(self):
        for name in NAMES:
            self.assertFalse((HIST.parent / name).exists(), name)
            self.assertTrue((HIST / name).exists(), name)


if __name__ == "__main__":
    unittest.main()
