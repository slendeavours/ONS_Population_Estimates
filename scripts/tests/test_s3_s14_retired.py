"""The three retired S3/S14 scripts stop at once, also when run directly and
with a broken environment, without opening a connection.
s14_lha_rates_build_v2.py is top-level code, so its guard also runs when it is
imported.

Every test that executes one of the scripts runs a copy of it in the scrubbed
sandbox of retired_sandbox.py: alone in a temporary directory (no _db, no repo
.env anywhere on its path), python -E -s, an environment built from nothing
(PATH and SystemRoot, dummy credentials, host invalid.invalid, port 1). If a
guard were ever lost, the script would fail at its first import or connection,
not repeat the 2026-10-11 incident against the real database.
"""

import ast
import re
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from retired_sandbox import run_retired, scrubbed_env  # noqa: E402

GUARD = "sys.exit(2)"
NEW_LOADER = {
    "s3_mye_refresh.py": "s3_mye_editions.py",
    "s14_lha_rates_build.py": "s14_lha_editions.py",
    "s14_lha_rates_build_v2.py": "s14_lha_editions.py",
}
NAMES = list(NEW_LOADER)


class Sandbox(unittest.TestCase):
    def test_sandbox_cannot_see_repo_env_or_db_module(self):
        import subprocess
        probe = chr(10).join([
            "import os",
            "try:",
            "    import _db",
            "    print('DB-MODULE-FOUND')",
            "except ImportError:",
            "    print('no _db')",
            "print(sorted(os.environ))",
            "print(os.environ.get('PG_HOST_OVERRIDE'), os.environ.get('PG_PORT'),",
            "      os.environ.get('PG_USER'))",
        ]) + chr(10)
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "probe.py"
            f.write_text(probe)
            r = subprocess.run([sys.executable, "-E", "-s", str(f)],
                               capture_output=True, text=True, cwd=d,
                               timeout=60, env=scrubbed_env())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no _db", r.stdout)
        self.assertNotIn("DB-MODULE-FOUND", r.stdout)
        self.assertIn("invalid.invalid 1 dummy_user", r.stdout)
        self.assertNotIn(str(SCRIPTS), r.stdout)
        keys = set(scrubbed_env())
        self.assertLessEqual(keys, {"PATH", "SystemRoot", "PG_HOST",
                                    "PG_HOST_OVERRIDE", "PG_PORT",
                                    "PG_DATABASE", "PG_USER", "PG_PASSWORD",
                                    "PYTHONDONTWRITEBYTECODE"})


class RetiredS3S14(unittest.TestCase):
    def _run(self, name, import_module=False):
        return run_retired(SCRIPTS / name, import_module=import_module)

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

    def test_retired_in_the_scrubbed_environment(self):
        for name in NAMES:
            with self.subTest(name):
                self._assert_retired(name, self._run(name))

    def test_v2_import_still_raises(self):
        r = self._run("s14_lha_rates_build_v2.py", import_module=True)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("RETIRED", r.stderr)
        self.assertIn("scripts/s14_lha_editions.py", r.stderr)
        self.assertNotIn("Traceback", r.stderr.replace("SystemExit", ""))

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

    def test_only_import_sys_and_the_guard_come_first(self):
        """After the docstring: 'import sys', then only the message assignment,
        the print to stderr and sys.exit(2). No other import, no other call."""
        for name in NAMES:
            tree = ast.parse((SCRIPTS / name).read_text(encoding="utf-8"))
            body = tree.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body = body[1:]
            with self.subTest(name):
                self.assertIsInstance(body[0], ast.Import)
                self.assertEqual([a.name for a in body[0].names], ["sys"])
                self.assertEqual(len(body), 4, ast.dump(tree)[:300])
                self.assertIsInstance(body[1], ast.Assign)
                self.assertIsInstance(body[1].value, ast.Constant)
                for node in (body[2], body[3]):
                    self.assertIsInstance(node, ast.Expr)
                    self.assertIsInstance(node.value, ast.Call)
                self.assertEqual(ast.unparse(body[2]),
                                 "print(_RETIRED, file=sys.stderr)")
                self.assertEqual(ast.unparse(body[3]), "sys.exit(2)")
                imports = [n for n in ast.walk(tree)
                           if isinstance(n, (ast.Import, ast.ImportFrom))]
                self.assertEqual(len(imports), 1)

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
