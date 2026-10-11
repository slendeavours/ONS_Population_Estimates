"""The three retired S3/S14 scripts stop at once, also when run directly
(scripts/ is then the script directory) and with a broken environment (an
invalid database host, a broken PYTHONPATH), without opening a connection.
s14_lha_rates_build_v2.py is top-level code, so its guard also runs when it is
imported."""

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
GUARD = "sys.exit(2)"
NEW_LOADER = {
    "s3_mye_refresh.py": "s3_mye_editions.py",
    "s14_lha_rates_build.py": "s14_lha_editions.py",
    "s14_lha_rates_build_v2.py": "s14_lha_editions.py",
}
NAMES = list(NEW_LOADER)


class RetiredS3S14(unittest.TestCase):
    def _run(self, name, env=None, args=None):
        cmd = [sys.executable] + (args or [str(SCRIPTS / name)])
        return subprocess.run(cmd, capture_output=True, text=True,
                              cwd=SCRIPTS, timeout=60, env=env)

    def _broken_env(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SCRIPTS / "nonexistent")
        for k in ("PGHOST", "PG_PASSWORD", "PGPASSWORD", "DATABASE_URL",
                  "PG_PORT", "PG_DATABASE", "PG_USER", "DB_HOST"):
            env.pop(k, None)
        env["PG_HOST"] = "invalid.invalid"
        env["PG_HOST_OVERRIDE"] = "invalid.invalid"
        return env

    def _assert_retired(self, name, r):
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertRegex(r.stderr, r"RETIRED 2026-10-1\d: use python scripts/"
                         + re.escape(NEW_LOADER[name])
                         + r" \(preview by default\)")
        self.assertEqual(r.stdout, "")
        for bad in ("Traceback", "ModuleNotFoundError", "could not translate host",
                    "HARD STOP", "ERROR:"):
            self.assertNotIn(bad, r.stderr)

    def test_run_directly_prints_retired_and_exits_2(self):
        for name in NAMES:
            with self.subTest(name):
                self._assert_retired(name, self._run(name))

    def test_retired_even_with_broken_environment(self):
        for name in NAMES:
            with self.subTest(name):
                self._assert_retired(name, self._run(name, self._broken_env()))

    def test_v2_import_still_raises(self):
        r = self._run("s14_lha_rates_build_v2.py",
                      args=["-c", "import s14_lha_rates_build_v2"])
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("RETIRED", r.stderr)
        self.assertIn("scripts/s14_lha_editions.py", r.stderr)
        self.assertNotIn("Traceback", r.stderr.replace("SystemExit", ""))
        r = self._run("s14_lha_rates_build_v2.py",
                      env=self._broken_env(),
                      args=["-c", "import s14_lha_rates_build_v2"])
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("RETIRED", r.stderr)

    def test_wording_names_what_each_script_did(self):
        want = {
            "s3_mye_refresh.py": [
                "id 86", "2026-08-13", "ON CONFLICT", "loaded_at", "no preview",
                "(lad24cd, reference_year)", "second time", "ids 2 to 16",
                "ONS Population Estimates (S3)"],
            "s14_lha_rates_build.py": [
                "ON CONFLICT", "no preview", "round()", "returned 0 for a blank",
                "X & Y", "imd2025.rank", "never executed", "2026-08-21"],
            "s14_lha_rates_build_v2.py": [
                "DROP TABLE", "CASCADE", "2026-08-20", "id 97", "top-level",
                "silently", "X & Y", "2026-08-21"],
        }
        for name, bits in want.items():
            r = self._run(name)
            for b in bits:
                with self.subTest(name=name, bit=b):
                    self.assertIn(b, r.stderr)

    def test_guard_is_first_statement_and_only_sys_is_imported_before_it(self):
        for name in NAMES:
            src = (SCRIPTS / name).read_text(encoding="utf-8")
            self.assertIn(GUARD, src)
            guard = src.index(GUARD)
            head = src[:guard]
            imports = [ln for ln in head.splitlines()
                       if ln.startswith(("import ", "from "))]
            with self.subTest(name):
                self.assertEqual(imports, ["import sys"])
            for mod in ("openpyxl", "pandas", "psycopg2", "_db", "requests",
                        "geopandas", "urllib", "argparse", "os"):
                for stmt in ("import " + mod, "from " + mod):
                    with self.subTest(name=name, mod=mod, stmt=stmt):
                        if "\n" + stmt in src:
                            self.assertLess(guard, src.index("\n" + stmt))

    def test_no_retired_body_left_in_the_files(self):
        for name in NAMES:
            src = (SCRIPTS / name).read_text(encoding="utf-8")
            with self.subTest(name):
                self.assertLess(len(src.splitlines()), 60)
                self.assertNotIn("INSERT INTO", src)
                self.assertNotIn("def task9_print_updated_w1_node5_sql", src)
                self.assertNotIn("LEFT JOIN la_imd_2025", src)

    def test_nothing_live_imports_or_runs_the_old_scripts(self):
        pat = re.compile(r"^\s*(import|from)\s+(s3_mye_refresh|s14_lha_rates_build)"
                         r"\b|python\s+\S*(s3_mye_refresh|s14_lha_rates_build)",
                         re.M)
        live = [p for p in SCRIPTS.glob("*.py") if p.name not in NAMES]
        live += [p for p in (SCRIPTS / "tests").glob("*.py")
                 if p.name != Path(__file__).name]
        for p in live:
            with self.subTest(p.name):
                text = p.read_text(encoding="utf-8")
                self.assertIsNone(pat.search(text), p.name)


if __name__ == "__main__":
    unittest.main()
