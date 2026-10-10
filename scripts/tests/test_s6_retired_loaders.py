"""The two retired S6 scripts (scripts/historical/s6_*.py) stop at once, also
when run directly (scripts/ is then not on sys.path) and with a broken
environment."""

import os
import subprocess
import sys
import unittest
from pathlib import Path

HIST = Path(__file__).resolve().parents[1] / "historical"
NAMES = ["s6_asylum_build.py", "s6_asylum_verify.py"]


class RetiredS6(unittest.TestCase):
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
                self.assertIn("s6_asylum_editions", r.stderr)
                self.assertIn("25 and 26 July 2026", r.stderr)
                self.assertIn("id 98", r.stderr)
                self.assertNotIn("ModuleNotFoundError", r.stderr)
                self.assertNotIn("Traceback", r.stderr)

    def test_retired_when_pandas_and_psycopg2_cannot_be_imported(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            for mod in ("pandas", "psycopg2", "requests", "dotenv"):
                Path(d, mod + ".py").write_text(
                    "raise ImportError('broken on purpose')" + chr(10))
            env = self._broken_env()
            env["PYTHONPATH"] = d
            for name in NAMES:
                with self.subTest(name):
                    r = self._run(name, env)
                    self.assertNotEqual(r.returncode, 0)
                    self.assertIn("RETIRED", r.stderr)
                    self.assertIn("s6_asylum_editions", r.stderr)
                    self.assertNotIn("broken on purpose", r.stderr)
                    self.assertNotIn("Traceback", r.stderr)

    def test_retired_even_with_broken_environment(self):
        for name in NAMES:
            with self.subTest(name):
                r = self._run(name, self._broken_env())
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("RETIRED", r.stderr)
                self.assertIn("s6_asylum_editions", r.stderr)
                self.assertNotIn("Traceback", r.stderr)

    def test_each_names_its_replacement(self):
        want = {
            "s6_asylum_build.py": "scripts/s6_asylum_editions.py",
            "s6_asylum_verify.py":
                "scripts/s6_asylum_editions_verify.py",
        }
        for name, repl in want.items():
            with self.subTest(name):
                r = self._run(name)
                self.assertIn("use " + repl, r.stderr)

    def test_wording_names_what_each_step_did(self):
        want = {
            "s6_asylum_build.py": ["ON CONFLICT", "DO UPDATE", "loaded_at",
                                   "no preview", "link text",
                                   "fixed temp names", "asylum_series_breaks",
                                   "CREATE OR REPLACE VIEW", "15 times",
                                   "ids 69 to 82", "id 98",
                                   "identical to those it replaced",
                                   "docs/s6_source_anomalies.md"],
            "s6_asylum_verify.py": ["rolled back", "same transaction",
                                    "docs/s6_source_anomalies.md",
                                    "hand-sourced anchor", "22 gates"],
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
            for mod in ("openpyxl", "pandas", "psycopg2", "requests", "dotenv", "_db",
                        "s6_asylum_"):
                for stmt in ("import " + mod, "from " + mod):
                    line_start = "\n" + stmt
                    if line_start in src:
                        with self.subTest(name=name, mod=mod):
                            self.assertLess(guard, src.index(line_start))

    def test_env_check_runs_after_the_guard(self):
        src = (HIST / "s6_asylum_build.py").read_text(encoding="utf-8")
        guard = src.index("sys.exit(_RETIRED)")
        self.assertLess(guard, src.index('user=_require_env("PG_USER")'))
        self.assertLess(guard, src.index("def _require_env"))

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
